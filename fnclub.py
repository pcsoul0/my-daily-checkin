import os
import re
import requests
import subprocess
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

# ====================== 配置区 ======================
FNOS_COOKIE = os.environ.get("FNOS_COOKIE", "")
# ====================================================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.95 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://club.fnnas.com/",
    "Connection": "keep-alive",
}


def write_result(content):
    try:
        beijing_tz = timezone(timedelta(hours=8))
        now = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"{now} - 飞牛论坛签到 {content}\n\n"
        with open("checkin_results.txt", "a", encoding="utf-8") as f:
            f.write(log_line)
        print("[INFO] 签到结果已追加写入 checkin_results.txt")
    except Exception as e:
        print(f"[ERROR] 写入失败: {e}")


def _load_cookies(session, cookie_str):
    """把 'k1=v1; k2=v2' 形式的 Cookie 串写入 session 的 cookie jar（按 .fnnas.com 域）。

    关键：必须用 jar 而不是 session.headers['Cookie']。requests 中一旦在
    headers 写死 Cookie，后续 session.cookies.set(...) 加进去的 cookie（如
    acw_sc__v2 / acw_tc）会被忽略、不随请求发送，导致 WAF 挑战重试失效。
    """
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        session.cookies.set(k.strip(), v.strip(), domain=".fnnas.com", path="/")


def _find_snippet(html, keyword, span=200):
    """返回 keyword 在 html 中前后各 span 字符的片段，用于定位拦截页长相。"""
    i = html.find(keyword)
    if i == -1:
        return ""
    return html[max(0, i - span): i + len(keyword) + span]


def diagnose(html):
    """返回页面特征，用于定位失败原因（是否登录、是否被 WAF 拦截等）。"""
    # WAF 类型细分：命中哪个标记就用哪个名字，便于推送通知一眼定位
    waf_type = "未知"
    waf_snippet = ""
    if "acw_sc__v2" in html:
        waf_type = "acw_sc__v2(Aliyun WAF Cookie挑战)"
        waf_snippet = _find_snippet(html, "acw_sc__v2")
    elif "unescape" in html and "document.cookie" in html:
        waf_type = "unescape+document.cookie(JS挑战)"
        waf_snippet = _find_snippet(html, "unescape")
    elif "安全验证" in html:
        waf_type = "安全验证(自定义验证页)"
        waf_snippet = _find_snippet(html, "安全验证")
    elif "滑动验证" in html:
        waf_type = "滑动验证(滑块验证码)"
        waf_snippet = _find_snippet(html, "滑动验证")

    feats = {
        "http_len": len(html),
        "logged_in": ("action=logout" in html) or ("退出" in html),
        "has_btna": 'class="btna"' in html,
        "already_signed": ("今日已打" in html) or ("今天已经" in html) or ("已签到" in html),
        "waf_challenge": waf_type != "未知",
        "waf_type": waf_type,
        "waf_snippet": waf_snippet,
        "need_login": ("logging" in html and "mod=logging" in html and "action=logout" not in html),
    }
    return feats


def log_diagnostics(resp):
    """把拦截页的关键信息写入结果文件，便于定位到底被什么拦。"""
    try:
        html = resp.text
        hdr_lines = []
        for k in ("Server", "Content-Type", "Date"):
            if k in resp.headers:
                hdr_lines.append(f"  {k}: {resp.headers[k]}")
        # Set-Cookie 只留名字 + 值前 24 字符预览，掩去完整敏感值
        for c in resp.cookies:
            val_preview = (c.value[:24] + "…") if len(c.value) > 24 else c.value
            hdr_lines.append(f"  Set-Cookie: {c.name}={val_preview}")
        # 任何含 waf/cf/challenge/verify 的响应头
        for k, v in resp.headers.items():
            if any(t in k.lower() for t in ("waf", "cf-", "challenge", "verify", "x-audit")):
                hdr_lines.append(f"  {k}: {v[:120]}")
        feats = diagnose(html)
        block = (
            "\n[诊断] HTTP={code} 最终URL={url}\n"
            "[诊断] 响应头:\n{hdrs}\n"
            "[诊断] WAF类型={wt}\n"
            "[诊断] WAF命中片段: {snip}\n"
            "[诊断] 页面前600字符:\n{page}\n"
        ).format(
            code=resp.status_code,
            url=resp.url,
            hdrs="\n".join(hdr_lines) if hdr_lines else "  (无关键头)",
            wt=feats.get("waf_type", "未知"),
            snip=(feats.get("waf_snippet", "") or "(无)")[:400],
            page=html[:600].replace("\n", " "),
        )
        write_result(block)
    except Exception as e:
        write_result(f"\n[诊断] 采集失败: {e}\n")


def solve_acw_sc__v2(challenge_html):
    """解 Aliyun WAF 的 acw_sc__v2 Cookie 挑战：抽取挑战页里的 <script>，
    在 Node 中执行站点自带的算法（含常量与 arg1 排列），捕获 document.cookie
    中的 acw_sc__v2 值。运行环境需有 node（GitHub Actions ubuntu-latest 自带）。
    返回 cookie 值字符串，失败返回 None。
    """
    node = shutil.which("node")
    if not node:
        print("[WARN] 未找到 node，无法解算 acw_sc__v2")
        return None
    m = re.search(r"<script[^>]*>(.*?acw_sc__v2.*?)</script>", challenge_html, re.S)
    if not m:
        print("[WARN] 挑战页中未找到含 acw_sc__v2 的 <script>")
        return None
    js = m.group(1)
    # 用 Proxy 桩接 document/window/navigator，让挑战 JS 能跑完并把 cookie 写进 _store
    wrapper = (
        "const _store={};"
        "function noop(){return undefined;}"
        "function mk(t){return new Proxy(t,{"
        "get(o,p){if(p==='cookie')return _store.cookie||'';if(p in o)return o[p];return noop;},"
        "set(o,p,v){if(p==='cookie'){_store.cookie=v;return true;}o[p]=v;return true;}});}"
        "var document=mk({location:mk({})});"
        "var window=mk({});var navigator=mk({});var self=window;"
        + js
        + ";try{var c=_store.cookie||'';var mm=c.match(/acw_sc__v2=([^;]+)/);"
        "process.stdout.write(mm?mm[1]:'');}catch(e){process.stdout.write('');}"
    )
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(wrapper)
            path = f.name
        out = subprocess.run([node, path], capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or None
    except Exception as e:
        print(f"[WARN] 解算 acw_sc__v2 异常: {e}")
        return None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _clean_text(html):
    """去掉 HTML 标签与多余空白，便于按关键词提取奖励文案。"""
    t = re.sub(r"<[^>]+>", " ", html)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _extract_reward(html):
    """从签到接口响应里尽量提取奖励信息（今日奖励积分等）。多模式兜底，取首个命中。"""
    clean = _clean_text(html)
    patterns = [
        r"今日奖励[^\d]{0,12}?(\d+)\s*(?:积分|分)?",
        r"连续签到[^\d]{0,20}?(\d+)[^\d]{0,8}?(?:积分|分)?",
        r"获得[^\d]{0,12}?(\d+)\s*(?:积分|分)",
        r"奖励[^\d]{0,12}?(\d+)\s*(?:积分|分)?",
        r"签到成功[^\d]{0,20}?(\d+)\s*(?:积分|分)?",
        r"恭喜[^\d]{0,20}?(\d+)\s*(?:积分|分)?",
    ]
    for p in patterns:
        m = re.search(p, clean)
        if m:
            return m.group(0).strip()
    # 兜底：定位第一个奖励相关关键词，返回其前后上下文（真实格式未知时人工核对/迭代正则）
    for kw in ["今日奖励", "连续签到", "签到成功", "获得", "奖励", "恭喜你"]:
        i = clean.find(kw)
        if i != -1:
            return clean[max(0, i - 20): i + 70].strip()
    return ""


def fnos_sign():
    if not FNOS_COOKIE:
        msg = "❌ 未获取到 FNOS_COOKIE，请检查环境变量"
        print(msg); write_result(msg); return

    session = requests.Session()
    session.headers.update(HEADERS)

    # 网络层重试：仅对连接错误 / 读取超时 / HTTP 5xx 重试（自愈偶发抖动），
    # 不对 4xx 重试（多为 cookie 失效等需人工处理的问题）。与下方 WAF(acw)
    # 业务层重试互不冲突，叠加工作。
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retry_strategy = Retry(
        total=2,                      # 最多重试 2 次（共 3 次请求）
        backoff_factor=1,             # 退避 1s / 2s
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        connect=2,
        read=2,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # 把 FNOS_COOKIE 解析进 jar（不要用 headers['Cookie']，否则会屏蔽后续 jar 里的 acw_sc__v2）
    _load_cookies(session, FNOS_COOKIE)

    sign_result = ""
    try:
        print("🔍 正在获取签到参数...")
        sign_url = "https://club.fnnas.com/plugin.php?id=zqlj_sign"
        resp = session.get(sign_url, timeout=(10, 30))
        resp.raise_for_status()
        # 强制用 GBK 解码，避免中文乱码影响判断
        if resp.encoding and resp.encoding.lower() in ("iso-8859-1", ""):
            resp.encoding = resp.apparent_encoding or "gbk"

        feats = diagnose(resp.text)
        print("[DEBUG] 页面特征:", feats)

        # 处理 Aliyun WAF acw_sc__v2 Cookie 挑战：解出 cookie 后重试一次
        if feats["waf_challenge"] and feats["waf_type"].startswith("acw_sc__v2"):
            print("🛡️ 检测到 acw_sc__v2 挑战，正在解算 Cookie...")
            acw = solve_acw_sc__v2(resp.text)
            if acw:
                # acw_tc / cdn_sec_tc 已在首次响应的 Set-Cookie 里，session 自动保留进 jar
                session.cookies.set("acw_sc__v2", acw, domain=".fnnas.com", path="/")
                print("✅ 已写入 acw_sc__v2，重试签到页...")
                print("[DEBUG] 重试将携带 Cookie 名:", "; ".join(sorted(session.cookies.keys())))
                resp = session.get(sign_url, timeout=(10, 30))
                resp.raise_for_status()
                if resp.encoding and resp.encoding.lower() in ("iso-8859-1", ""):
                    resp.encoding = resp.apparent_encoding or "gbk"
                feats = diagnose(resp.text)
                print("[DEBUG] 重试后页面特征:", feats)
            else:
                sign_result = "❌ 签到失败：acw_sc__v2 解算失败（未找到 node 或挑战脚本异常）"
                print(sign_result); write_result(sign_result); return

        sign_match = re.search(r'sign&sign=(.+?)" class="btna', resp.text)

        if not sign_match:
            # 先采集拦截页诊断信息（HTTP/URL/响应头/页面片段），再细分失败原因
            log_diagnostics(resp)
            if feats["waf_challenge"]:
                sign_result = f"❌ 签到失败：被网站防火墙/人机验证拦截（WAF类型：{feats['waf_type']}；Runner 出口 IP 可能被判定异常）"
            elif feats["need_login"] or not feats["logged_in"]:
                sign_result = "❌ 签到失败：Cookie 已失效（服务器返回未登录页），请更新 FNOS_COOKIE secret"
            elif feats["already_signed"]:
                sign_result = "✅ 今日已签到（页面无签到按钮）"
            else:
                sign_result = "❌ 签到失败：未找到签到按钮，页面结构可能已变化"
            print(sign_result); write_result(sign_result); return

        sign_code = sign_match.group(1)
        # 若已签到：按钮文本会是“今日已打卡”，此时 sign 仍带旧 code，避免重复提交
        btn_html = re.search(r'sign&sign=.+?" class="btna"[^>]*>([^<]+)<', resp.text)
        btn_text = btn_html.group(1) if btn_html else ""
        # 注意：状态只认按钮文字，不再参考 feats["already_signed"]（该字段会因页面签到规则/统计文案中的
        # “已签到”字样而恒为真，导致把“点击打卡”误判为已签到）
        already = ("已打卡" in btn_text) or ("今日已打" in btn_text) or ("已签到" in btn_text)
        if already:
            sign_result = f"✅ 今日已签到（按钮：{btn_text.strip()}）"
            print(sign_result); write_result(sign_result); return

        print(f"✅ 获取签到参数成功：{sign_code}（按钮：{btn_text}）")

        print("🚀 正在执行签到...")
        do_sign_url = f"https://club.fnnas.com/plugin.php?id=zqlj_sign&sign={sign_code}"
        resp2 = session.get(do_sign_url, timeout=(10, 30))
        resp2.raise_for_status()
        f2 = diagnose(resp2.text)
        print("[DEBUG] 签到接口响应特征:", f2)
        if not (f2["already_signed"] or f2["logged_in"]):
            print("⚠️ 签到接口未返回预期内容，可能被拦截")
        # 抓取签到接口反馈的奖励信息（今日奖励积分等），写进结果文件
        reward = _extract_reward(resp2.text)
        # 打印奖励相关上下文（更宽窗口，便于真实返回格式未知时迭代正则）
        _dbg = _clean_text(resp2.text)
        for _kw in ["今日奖励", "连续签到", "签到成功", "获得", "奖励", "恭喜"]:
            _j = _dbg.find(_kw)
            if _j != -1:
                print("[DEBUG] 签到接口奖励上下文:", _dbg[max(0, _j - 40): _j + 100].strip())
                break
        if reward:
            print("[DEBUG] 签到接口奖励识别:", reward)

        print("📊 正在获取账户信息...")
        info_url = "https://club.fnnas.com/home.php?mod=spacecp&ac=credit&showcredit=1"
        resp3 = session.get(info_url, timeout=(10, 30))
        if resp3.encoding and resp3.encoding.lower() in ("iso-8859-1", ""):
            resp3.encoding = resp3.apparent_encoding or "gbk"

        fnb = re.search(r'飞牛币: </em>(\d+)', resp3.text)
        nz = re.search(r'牛值: </em>(\d+)', resp3.text)
        ts = re.search(r'登陆天数: </em>(\d+)', resp3.text)
        jf = re.search(r'积分: </em>(\d+)', resp3.text)

        fnb = fnb.group(1) if fnb else "获取失败"
        nz = nz.group(1) if nz else "获取失败"
        ts = ts.group(1) if ts else "获取失败"
        jf = jf.group(1) if jf else "获取失败"

        reward_part = f" | 今日奖励：{reward}" if reward else ""
        sign_result = f"成功 | 飞牛币：{fnb} | 牛值：{nz} | 登录天数：{ts} | 积分：{jf}{reward_part}"
        print("\n" + sign_result)

    except Exception as e:
        sign_result = f"❌ 签到异常：{str(e)}"
        print(sign_result)

    write_result(sign_result)


if __name__ == "__main__":
    print("=" * 40)
    print("        FNOS 论坛自动签到脚本启动")
    print("=" * 40)
    fnos_sign()
