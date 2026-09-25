# -*- coding: utf-8 -*-
"""
恩山论坛签到 —— Sitoi 纯 HTTP 方案（实测验证版）
不依赖浏览器（无 ChromiumPage / DrissionPage），不随机长等。
思路来自 Sitoi/dailycheckin（https://github.com/Sitoi/dailycheckin）。
日志格式与原 enshan.py 保持一致，直接追加写入 checkin_results.txt。
"""
import json
import os
import re
import requests
from datetime import datetime, timedelta, timezone

USER_AGENT = ("Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36")

BASE = "https://www.right.com.cn/forum"
SIGN_IN_URL = f"{BASE}/erling_qd-sign_in_m.html"
FORUM_URL = f"{BASE}/forum.php?mobile=2"
SIGN_API = f"{BASE}/plugin.php?id=erling_qd:action&action=sign"
CREDIT_URL = f"{BASE}/home.php?mod=spacecp&ac=credit&op=log&mobile=2"
PROFILE_URL_TPL = f"{BASE}/home.php?mod=space&uid={{uid}}&do=profile&mycenter=1&mobile=2"


def write_result(content):
    try:
        beijing_tz = timezone(timedelta(hours=8))
        now = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"{now} - {content}\n\n"
        with open("checkin_results.txt", "a", encoding="utf-8") as f:
            f.write(log_line)
        print("[INFO] 签到结果已追加写入 checkin_results.txt")
    except Exception as e:
        print(f"[ERROR] 写入 checkin_results.txt 失败: {e}")


def extract_regex(pattern, text, default="0"):
    try:
        m = re.search(pattern, text)
        return m.group(1).strip() if m else default
    except Exception:
        return default


def run_sign_in():
    raw_cookie = os.getenv("ESHAN_COOKIE", "")
    user_uid = os.getenv("USER_UID", "")
    if not raw_cookie or not user_uid:
        msg = "❌ 错误: 环境变量 ESHAN_COOKIE / USER_UID 配置缺失"
        print(msg)
        write_result(msg)
        return

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": BASE + "/",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": raw_cookie,
    })
    # 跳过证书校验（恩山偶发证书链问题），与浏览器行为趋近
    session.verify = False
    import urllib3
    urllib3.disable_warnings()

    try:
        print("=== 开始执行恩山签到 (Sitoi 纯 HTTP 方案) ===")

        # 1. 取 formhash（移动端签到页优先，forum 兜底）
        print("1. 获取签到页与 formhash ...")
        html = session.get(SIGN_IN_URL, timeout=30).text
        formhash = extract_regex(r"var FORMHASH = '([0-9a-zA-Z]+)'", html, "")
        if not formhash:
            formhash = extract_regex(r'name="formhash" value="([0-9a-zA-Z]+)"', html, "")
        if not formhash:
            formhash = extract_regex(r'formhash=([0-9a-zA-Z]+)', html, "")
        if not formhash:
            fh2 = session.get(FORUM_URL, timeout=30).text
            formhash = extract_regex(r'name=["\']formhash["\']\s+value=["\']([0-9a-zA-Z]+)["\']', fh2, "")

        # 是否已签到 / Cookie 是否失效
        is_signed = ("连续签到" in html) and ("立即签到" not in html)
        if is_signed:
            print("ℹ️ 今天已经签到过了。")

        if not formhash and not is_signed:
            if "登录" in html or "login" in html.lower():
                msg = "❌ 严重错误: Cookie 已失效，变为游客状态。"
                print(msg); write_result(msg); return
            msg = "❌ 错误: 无法提取 formhash（可能触发 WAF，建议回退浏览器方案）"
            print(msg); write_result(msg); return

        if formhash:
            print(f"🔑 获取 Formhash 成功: {formhash}")

        # 2. 执行签到
        sign_success = is_signed
        sign_msg = "已签到"
        if not is_signed and formhash:
            print("🚀 正在发送签到请求 ...")
            r = session.post(SIGN_API, data={"formhash": formhash}, timeout=30)
            try:
                result = r.json()
                print(f"📥 签到接口返回: {result}")
                if result.get("success") or "已经签到" in str(result):
                    sign_success = True
                    sign_msg = result.get("message", "签到成功")
                else:
                    sign_msg = result.get("message", "未知错误")
            except Exception:
                sign_msg = "接口返回非 JSON（可能触发 WAF）"
                print(f"⚠️ {sign_msg}: {r.status_code} / {r.text[:200]}")

        if not sign_success:
            msg = f"❌ 恩山论坛签到失败：{sign_msg}"
            print(msg); write_result(msg); return

        # 3. 抓取积分数据
        print("4. 正在获取最终积分数据 ...")
        s2 = session.get(SIGN_IN_URL, timeout=30).text
        today_points = extract_regex(r'erqd-current-point[^>]*>(\d+)', s2, "未知")
        continuous_days = extract_regex(r'erqd-continuous-days[^>]*>(\d+)', s2, "未知")
        total_days = extract_regex(r'erqd-total-days[^>]*>(\d+)', s2, "未知")

        p = session.get(PROFILE_URL_TPL.format(uid=user_uid), timeout=30)
        total_points = contribution = enshan_coin = "未知"
        try:
            html = p.text
            for li in re.findall(r"<li[^>]*>(.*?)</li>", html, re.S):
                # 关键修复：先剥离 <li> 内部所有 HTML 标签，得到纯文本
                # （requests 返回的是生 HTML，原版用浏览器渲染后的 .text 才有此效果）
                txt = re.sub(r"<[^>]+>", "", li)
                txt = re.sub(r"\s+", "", txt)
                if not txt:
                    continue
                # 调试：打印含关键字的 li，便于核对页面真实结构
                if any(k in txt for k in ["积分", "Points", "贡献", "Contributions", "恩山币", "EnshanCoin"]):
                    print(f"   [li] {txt[:60]}")
                # 总积分（排除“今日积分”）
                if ("积分" in txt and "今日" not in txt) or "Points" in txt:
                    m = re.search(r"(\d+)", txt)   # 位置无关：积分12345 / 12345积分 都能抓
                    if m: total_points = m.group(1)
                if "贡献" in txt or "Contributions" in txt:
                    m = re.search(r"(\d+)", txt)
                    if m: contribution = m.group(1)
                if "恩山币" in txt or "EnshanCoin" in txt:
                    m = re.search(r"(\d+)", txt)
                    if m: enshan_coin = m.group(1)
            print(f"📊 抓取结果: 积分={total_points}, 贡献={contribution}, 币={enshan_coin}")
        except Exception as e:
            print(f"❌ 数据解析异常: {e}")

        notify = (f"✅ 恩山论坛签到成功(Sitoi方案) | 今日积分：{today_points} | "
                  f"连续签到：{continuous_days}天 | 总天数：{total_days}天 | "
                  f"总积分：{total_points} | 贡献：{contribution} | 恩山币：{enshan_coin}")
        print("=== 签到结果 ===")
        print(notify)
        write_result(notify)

    except Exception as e:
        import traceback
        traceback.print_exc()
        error_msg = f"❌ 恩山脚本运行出错: {str(e)}"
        print(error_msg)
        write_result(error_msg)


if __name__ == "__main__":
    run_sign_in()
