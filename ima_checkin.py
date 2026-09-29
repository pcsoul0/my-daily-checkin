#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""腾讯 ima 每日登录领算力（GitHub Actions 定时运行版）

并入 my-daily-checkin：由 sign_all.yml 统一调度（北京时间 00:09），
结果追加写入 checkin_results.txt，交给 daily_push.py 汇总推送 PushPlus。

逆向结论（2026-09-25/26，来源：ima 网页 SPA 反编译 + 实测验证）：
1. 活动走独立服务 daily_login_activity（区别于 activity_center 活动系统）：
   - 查询: POST https://ima.qq.com/cgi-bin/daily_login_activity/get_activity_info  body {}
   - 签到: POST https://ima.qq.com/cgi-bin/daily_login_activity/check_in
       body {"type": 1} 每日签到  |  {"type": 2} 连签 7 天的满签奖励
2. 认证头三件套：
   - x-ima-cookie: cookie 串（11 个 K=V 字段，分号分隔）
   - x-ima-bkn: 由 IMA-TOKEN 做 DJB2 变体哈希实时计算（已与抓包值比对一致）
   - from_browser_ima: 1  （防伪头，缺失返回 code 41）
3. 业务码：0 成功；1002 "today already checked in"（幂等，视为成功）；
   1100/1101/600001 认证失效。
4. 活动状态（get_activity_info → infos[]）：status 2=已领取 3=今日可领 4=待解锁；
   top=="今日" 为每日签到槽位，top=="满签奖励" 为周奖励槽位。

token 生命周期（2026-09-26 从主站 bundle + localStorage 实测确认）：
- access token（IMA-TOKEN）：固定 TTL 7200s（2 小时），accountInfo.tokenValidTime=7200；
- refresh token：有效期 2592000s（30 天），accountInfo.refreshTokenValidTime；
- 刷新端点：POST /cgi-bin/auth_login/refresh，body {"user_id","refresh_token","token_type"}
  （LoginHttpService.refresh，IsUnLoginTag 服务，无需有效 access token 即可调用）；
  响应 {code:0, token:"...", token_valid_time:"7200"}；实测 refreshToken 不轮换；
- 据此：IMA_REFRESH Secret（JSON）长期有效，脚本每次运行先刷新再签到，约 30 天
  才需重新扫码一次（若服务端轮换 refreshToken 或到期，会在日志中明确提示）。

设计要点：
- 先刷新后签到：IMA_REFRESH 模式下每次运行换新 access token，彻底绕开 2h TTL；
- 兼容回退：无 IMA_REFRESH 时回退旧的 IMA_COOKIE 模式（token 随时可能已过期）；
- 先查后签：仅当槽位 status==3 才调用 check_in；
- 满签延迟解锁适配：连签第 7 天签到后满签槽位解锁有服务端延迟（实测数分钟），
  签到动作完成后重查状态；若连签满 7 的倍数且槽位仍锁定，等待重查
  （30s × 4），解锁即自动领取 type=2；若窗口内仍未解锁，下次运行自动补领；
- code=1002 视为"已签成功"（与 workbuddy_checkin.py 的幂等适配同思路）；
- 瞬时网络错误重试 3 次；结果写入 GITHUB_STEP_SUMMARY 与 checkin_results.txt；
  全程不打印凭据本体。

退出码：0 成功/已签 | 2 配置缺失 | 4 网络失败 | 5 API 拒绝（含凭据失效）
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

from logsafe import mask_secret, redact, redact_obj

# 网关对默认 UA 不友好，统一带浏览器 UA（与 workbuddy_checkin.py 同策略）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

API_BASE = "https://ima.qq.com/cgi-bin/daily_login_activity"
AUTH_REFRESH_URL = "https://ima.qq.com/cgi-bin/auth_login/refresh"
REFERER = "https://ima.qq.com/copilot-token-daily-checkin/pc"
MAX_RETRY = 3
RETRY_INTERVAL = 5
RESULTS_FILE = "checkin_results.txt"   # 汇总结果文件（由 daily_push.py 读取后推送 PushPlus）

# x-ima-cookie 字段顺序（与 2026-09-25 抓包一致；静态字段来自扫码时的设备指纹）
COOKIE_FIELD_ORDER = [
    "PLATFORM", "CLIENT-TYPE", "WEB-VERSION", "IMA-GUID", "IMA-Q36",
    "IMA-IUA", "IMA-UID", "IMA-TOKEN", "IMA-REFRESH-TOKEN", "UID-TYPE", "TOKEN-TYPE",
]

# 业务码
CODE_SUCCESS = 0
CODE_ALREADY = 1002          # "today already checked in"
CODE_AUTH_EXPIRED = {1100, 1101, 600001}  # 认证失效/登录过期

# 槽位状态（bundle 枚举：Unspecified=0 Expired=1 Received=2 Available=3 Locked=4）
ST_RECEIVED, ST_AVAILABLE, ST_LOCKED = 2, 3, 4

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def to_int32(x):
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def bkn(token):
    """x-ima-bkn 算法（SPA bundle 中 l_ 函数的 Python 等价实现，DJB2 变体）：
    JS: let t=5381; for(...) t += (t<<5) + charCode; return t & 2147483647;
    JS 的 << 会先 ToInt32，故每轮需模拟 int32 回绕。已用 2026-09-25 抓包值验证一致。"""
    t = 5381
    for ch in token:
        t = t + (to_int32(t) << 5) + ord(ch)
    return to_int32(t) & 0x7FFFFFFF


def write_result(content):
    """把本轮结果追加写入 checkin_results.txt（与 enshan.py 等脚本同格式），
    由 daily_push.py 汇总推送 PushPlus。该文件已被 .gitignore 忽略，不入库。"""
    try:
        import datetime
        beijing = datetime.timezone(datetime.timedelta(hours=8))
        now = datetime.datetime.now(beijing).strftime("%Y-%m-%d %H:%M:%S")
        with open(RESULTS_FILE, "a", encoding="utf-8") as f:
            f.write(f"{now} - {content}\n\n")
        print("[信息] 签到结果已追加写入 checkin_results.txt")
    except Exception as e:
        print(f"[错误] 写入 checkin_results.txt 失败: {e}")


def summary_write(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)


# ---------------------------------------------------------------- 凭据装载

def load_refresh_cred():
    """读 IMA_REFRESH 凭据（JSON）：环境变量优先，其次 ima_refresh.info 文件。
    JSON 结构：{"user_id","refresh_token","token_type","static_fields":{...}}"""
    raw = os.environ.get("IMA_REFRESH", "").strip()
    src = "环境变量 IMA_REFRESH"
    if not raw:
        path = os.environ.get("IMA_REFRESH_FILE") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ima_refresh.info")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            src = f"文件 {os.path.basename(path)}"
    if not raw:
        return None, "未找到"
    try:
        cred = json.loads(raw)
        for key in ("user_id", "refresh_token"):
            if not cred.get(key):
                return None, f"{src}（缺少 {key}）"
        return cred, src
    except Exception as e:
        return None, f"{src}（JSON 解析失败: {e}）"


def load_cookie():
    """旧模式凭据：完整 x-ima-cookie 串。环境变量 IMA_COOKIE 优先；
    否则读 IMA_COOKIE_FILE 或脚本旁 ima_cookie.info。"""
    cookie = os.environ.get("IMA_COOKIE", "").strip()
    if cookie:
        return cookie, "环境变量"
    path = os.environ.get("IMA_COOKIE_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ima_cookie.info")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            cookie = f.read().strip()
        if cookie:
            return cookie, f"文件 {os.path.basename(path)}"
    return "", "未找到"


def extract_imatoken(cookie):
    """从 x-ima-cookie 串中提取 IMA-TOKEN（bkn 计算的输入）。"""
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("IMA-TOKEN="):
            return part.split("=", 1)[1]
    return ""


def refresh_access_token(cred):
    """调 /cgi-bin/auth_login/refresh 换新 access token。
    返回 (new_token, rotated_refresh_token 或 None, err)。
    该端点为 IsUnLoginTag 服务（bundle 中 LoginHttpService.refresh），
    无需有效 access token；瞬时错误重试 3 次。"""
    body = {
        "user_id": cred["user_id"],
        "refresh_token": cred["refresh_token"],
        "token_type": int(cred.get("token_type", 14)),
    }
    last_err = ""
    for attempt in range(1, MAX_RETRY + 1):
        req = urllib.request.Request(
            AUTH_REFRESH_URL, data=json.dumps(body).encode("utf-8"), method="POST")
        req.add_header("content-type", "application/json")
        req.add_header("from_browser_ima", "1")
        req.add_header("user-agent", UA)
        req.add_header("referer", "https://ima.qq.com/")
        req.add_header("origin", "https://ima.qq.com")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            if data.get("code") == CODE_SUCCESS and data.get("token"):
                return data["token"], data.get("refresh_token"), None
            # 业务拒绝：refresh token 失效等，不重试
            code = data.get("code")
            msg = data.get("msg", "")
            return None, None, f"刷新被拒绝 code={code} msg={msg}"
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw)
                code, msg = data.get("code"), data.get("msg", "")
                return None, None, f"刷新被拒绝 HTTP {e.code} code={code} msg={msg}"
            except Exception:
                last_err = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = f"网络异常 {e.__class__.__name__}"
        if attempt < MAX_RETRY:
            time.sleep(RETRY_INTERVAL)
    return None, None, f"刷新失败（重试耗尽）：{last_err}"


def build_cookie(cred, access_token, refresh_token):
    """按抓包字段顺序构造 x-ima-cookie 串。静态设备字段取自凭据 static_fields；
    IMA-UID 用 user_id；token 字段用刷新所得新值。"""
    static_fields = cred.get("static_fields") or {}
    fields = dict(static_fields)  # PLATFORM/CLIENT-TYPE/WEB-VERSION/IMA-GUID/IMA-Q36/IMA-IUA/UID-TYPE
    fields["IMA-UID"] = cred["user_id"]
    fields["IMA-TOKEN"] = access_token
    fields["IMA-REFRESH-TOKEN"] = refresh_token
    fields.setdefault("UID-TYPE", "1")
    fields["TOKEN-TYPE"] = str(int(cred.get("token_type", 14)))
    return "; ".join(f"{k}={fields[k]}" for k in COOKIE_FIELD_ORDER if k in fields)


def make_session():
    """准备 (cookie, token, 描述)。优先 IMA_REFRESH 刷新模式，回退 IMA_COOKIE。
    返回 (cookie, token, describe_str, err)。"""
    cred, src = load_refresh_cred()
    if src != "未找到" and cred is None:
        # IMA_REFRESH 存在但不可用：大声报错，避免静默回退到注定过期的旧 cookie 模式
        print(f"⚠️ 检测到 IMA_REFRESH 但无法使用：{src}")
    if cred:
        print(f"凭据来源: {src}（refresh 模式）；refresh_token {mask_secret(cred['refresh_token'])}")
        new_token, rotated, err = refresh_access_token(cred)
        if err:
            return "", "", "", f"IMA_REFRESH 刷新失败：{err}"
        rt = rotated or cred["refresh_token"]
        if rotated and rotated != cred["refresh_token"]:
            print("⚠️ 服务端轮换了 refreshToken（本次仍可签到；自动化依赖其长期有效，"
                  "若频繁出现需改用扫码更新方案）")
        cookie = build_cookie(cred, new_token, rt)
        print(f"access token 已刷新 {mask_secret(new_token)}")
        return cookie, new_token, "refresh 模式（IMA_REFRESH）", None

    cookie, src = load_cookie()
    if not cookie:
        return "", "", "", "未找到 IMA_REFRESH / IMA_COOKIE（环境变量或 info 文件）"
    token = extract_imatoken(cookie)
    if not token:
        return "", "", "", "cookie 中未包含 IMA-TOKEN 字段"
    print(f"凭据来源: {src}（旧 cookie 模式）；IMA-TOKEN {mask_secret(token)}")
    return cookie, token, "旧 cookie 模式（IMA_COOKIE）", None


# ---------------------------------------------------------------- 业务请求

def post(endpoint, body, cookie, token):
    """POST 请求，返回 (http_status, dict|None)。瞬时错误由调用方重试。"""
    url = f"{API_BASE}/{endpoint}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("x-ima-cookie", cookie)
    req.add_header("x-ima-bkn", str(bkn(token)))
    req.add_header("from_browser_ima", "1")
    req.add_header("user-agent", UA)
    req.add_header("referer", REFERER)
    req.add_header("origin", "https://ima.qq.com")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # 4xx/5xx 也读出 body 供业务判断（与网关用 HTTP 状态码承载业务码的场景兼容）
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, None


def post_retry(endpoint, body, cookie, token):
    """瞬时网络错误（无响应/5xx）重试；4xx 业务响应不重试。返回 (status, data, err)。"""
    for attempt in range(1, MAX_RETRY + 1):
        status, data = post(endpoint, body, cookie, token)
        if status is None:
            err = f"网络异常（第 {attempt}/{MAX_RETRY} 次）"
            print(redact(err))
            if attempt < MAX_RETRY:
                time.sleep(RETRY_INTERVAL)
            continue
        if status >= 500:
            err = f"服务端 {status}（第 {attempt}/{MAX_RETRY} 次）"
            print(redact(err))
            if attempt < MAX_RETRY:
                time.sleep(RETRY_INTERVAL)
            continue
        return status, data, None
    return None, None, "重试耗尽"


def classify_result(code):
    """业务码 → (是否成功, 人类可读说明)"""
    if code == CODE_SUCCESS:
        return True, "签到成功"
    if code == CODE_ALREADY:
        return True, "今日已签（幂等）"
    if code in CODE_AUTH_EXPIRED:
        return False, ("认证失效（refresh 模式下不应出现；若出现说明 refresh token "
                       "已被服务端吊销，需重新扫码更新 IMA_REFRESH）")
    return False, f"API 拒绝 code={code}"


# ---------------------------------------------------------------- 主流程

def refresh_infos(cookie, token):
    """重新查询活动状态（签到动作后刷新槽位解锁状态）。
    返回 (infos, days, total, err)。"""
    s, d, e = post_retry("get_activity_info", {}, cookie, token)
    if e:
        return None, None, None, e
    if not isinstance(d, dict) or d.get("code") != CODE_SUCCESS:
        return None, None, None, f"code={(d or {}).get('code')} msg={(d or {}).get('msg', '')}"
    return d.get("infos") or [], d.get("checkin_days"), d.get("total_reward_points"), None


def claim_bonus(cookie, token, actions):
    """领取满签奖励 check_in(type=2)。返回退出码增量（0 成功 / 4 网络 / 5 拒绝）。"""
    s, d, e = post_retry("check_in", {"type": 2}, cookie, token)
    code = (d or {}).get("code")
    if e:
        actions.append(f"❌ 满签奖励网络失败：{e}")
        return 4
    ok, msg = classify_result(code if code is not None else -9999)
    reward = (d or {}).get("reward_received") or (d or {}).get("rewardReceived")
    if reward is not None:
        msg += f"，+{reward} 算力"
    actions.append(("✅ 满签奖励：" if ok else "❌ 满签奖励：") + msg)
    return 0 if ok else 5


def main():
    cookie, token, desc, err = make_session()
    if err:
        print("ERROR:", redact(err))
        summary_write("## ima 每日登录领算力\n\n- ❌ " + err)
        write_result("❌ ima 每日登录领算力：" + err)
        return 2 if "未找到" in err else 5

    # ---- 1. 查询活动状态 ----
    status, info, err = post_retry("get_activity_info", {}, cookie, token)
    if err:
        summary_write("## ima 每日登录领算力\n\n- ❌ 查询失败：" + err)
        write_result("❌ ima 每日登录领算力：查询失败（" + err + "）")
        return 4
    if not isinstance(info, dict) or info.get("code") not in (CODE_SUCCESS,):
        code = (info or {}).get("code")
        ok, msg = classify_result(code if code is not None else -9999)
        summary_write(f"## ima 每日登录领算力\n\n- ❌ 查询失败：{msg}")
        write_result(f"❌ ima 每日登录领算力：查询失败（{msg}）")
        print("查询响应:", redact_obj(info, 500))
        return 5 if ok is False else 4

    infos = info.get("infos") or []
    days = info.get("checkin_days")
    total = info.get("total_reward_points")

    exit_code = 0
    actions = []
    signed_today = False  # 本次运行是否实际执行了每日签到 check_in(type=1)

    # ---- 2. 每日签到（top=="今日" 且 status==3 才签）----
    today = next((it for it in infos if it.get("top") == "今日"), None)
    if today is None:
        actions.append("⚠️ 响应中无「今日」槽位，跳过签到（需人工核对响应结构）")
        exit_code = exit_code or 5
    elif today.get("status") == ST_AVAILABLE:
        signed_today = True
        s2, d2, e2 = post_retry("check_in", {"type": 1}, cookie, token)
        code = (d2 or {}).get("code")
        if e2:
            actions.append(f"❌ 每日签到网络失败：{e2}")
            exit_code = exit_code or 4
        else:
            ok, msg = classify_result(code if code is not None else -9999)
            reward = (d2 or {}).get("reward_received") or (d2 or {}).get("rewardReceived")
            if reward is not None:
                msg += f"，+{reward} 算力"
            actions.append(("✅ 每日签到：" if ok else "❌ 每日签到：") + msg)
            if not ok:
                exit_code = 5
    elif today.get("status") == ST_RECEIVED:
        actions.append("✅ 每日签到：今日已领取")
    else:
        actions.append(f"ℹ️ 每日签到：今日槽位 status={today.get('status')}（非可领状态），跳过")

    # ---- 3. 满签奖励（top=="满签奖励" 且 status==3 时 type=2）----
    # 关键场景：连签第 7 天签到成功后，满签槽位的解锁存在服务端延迟——
    # 第 1 步查询拿到的槽位可能仍是 status=4（2026-09-27 实测漏领后补修复）。
    # 因此：每日签到动作完成后重查一次；若连签恰满 7 的倍数且槽位仍锁定，
    # 进入等待-重查窗口（30s × 4 ≈ 2 分钟），解锁即领。
    bonus = next((it for it in infos if it.get("top") == "满签奖励"), None)
    if bonus is not None and bonus.get("status") == ST_AVAILABLE:
        exit_code = exit_code or claim_bonus(cookie, token, actions)
    elif (bonus is not None and bonus.get("status") == ST_LOCKED
          and signed_today and isinstance(days, int) and days > 0 and days % 7 == 0):
        unlocked = False
        for attempt in range(1, 5):
            print(f"[满签] 连签满 {days} 天，奖励槽位未解锁，{30}s 后重查（{attempt}/4）...")
            time.sleep(30)
            new_infos, new_days, new_total, rerr = refresh_infos(cookie, token)
            if rerr is not None:
                print(f"[满签] 重查失败：{redact(rerr)}")
                continue
            infos, days, total = new_infos, new_days, new_total
            bonus = next((it for it in infos if it.get("top") == "满签奖励"), None)
            if bonus is not None and bonus.get("status") == ST_AVAILABLE:
                unlocked = True
                break
        if unlocked:
            exit_code = exit_code or claim_bonus(cookie, token, actions)
        else:
            actions.append("⚠️ 连签满 7 天但满签奖励 2 分钟内未解锁（服务端延迟）；"
                           "槽位解锁后任意一次运行本脚本都会自动补领")
    if bonus is not None and not any(a.startswith(("✅ 满签奖励", "❌ 满签奖励", "⚠️ 连签满 7 天")) for a in actions):
        actions.append(f"ℹ️ 满签奖励：{bonus.get('button')}（status={bonus.get('status')}）")

    # ---- 4. 汇总输出（表格用最新状态；签到/领取动作可能已改变天数与累计值）----
    lines = ["## ima 每日登录领算力",
             "",
             f"- 连续签到 **{days}** 天，累计领取 **{total}** 算力",
             "| 槽位 | 状态 | 奖励 |",
             "|---|---|---|"]
    for it in infos:
        lines.append(f"| {it.get('top')} | {it.get('button')}（status={it.get('status')}） | {it.get('reward')} |")
    lines.append("")
    lines.extend(f"- {a}" for a in actions)
    summary_write("\n".join(lines))

    # 汇总一行写入 checkin_results.txt，交给 daily_push.py 推送 PushPlus
    tag = "✅" if exit_code == 0 else "❌"
    detail = "；".join(actions) if actions else "无动作"
    write_result(f"{tag} ima 每日登录领算力 | 连续 {days} 天，累计 {total} 算力 | {detail}")
    return exit_code


def probe():
    """探测模式（--probe）：验证凭据链路健康（refresh 模式：刷一次 token + 只读查询；
    cookie 模式：直接只读查询）。一律退出码 0，不触发邮件。"""
    import datetime
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    cookie, token, desc, err = make_session()
    if err:
        summary_write(f"PROBE_RESULT {now_utc:%Y-%m-%dT%H:%M:%SZ} STATUS=CRED_FAIL DETAIL={err}")
        return 0

    status, info, err = post_retry("get_activity_info", {}, cookie, token)
    code = (info or {}).get("code")
    if err:
        line = f"PROBE_RESULT {now_utc:%Y-%m-%dT%H:%M:%SZ} STATUS=NETWORK_ERROR DETAIL={err}"
    elif code == CODE_SUCCESS:
        days = (info or {}).get("checkin_days")
        line = f"PROBE_RESULT {now_utc:%Y-%m-%dT%H:%M:%SZ} STATUS=ALIVE DETAIL=checkin_days={days}"
    elif code in CODE_AUTH_EXPIRED:
        line = f"PROBE_RESULT {now_utc:%Y-%m-%dT%H:%M:%SZ} STATUS=EXPIRED DETAIL=code={code}"
    else:
        line = f"PROBE_RESULT {now_utc:%Y-%m-%dT%H:%M:%SZ} STATUS=OTHER DETAIL=code={code}"
    summary_write(line)
    return 0


if __name__ == "__main__":
    if "--probe" in sys.argv:
        sys.exit(probe())
    sys.exit(main())
