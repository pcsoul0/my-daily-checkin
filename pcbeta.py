# -*- coding: utf-8 -*-
"""
远景论坛(pcbeta) 每日签到 —— 纯 HTTP + cookie 登录

实测结论（2026-09-03）：pcbeta 已升级反爬，凡来自机房 IP 的请求均被
「异常请求验证」滑块验证码 + 验证问卷拦截（HTTP 403），纯 HTTP 无法自动通过。
本脚本保留请求逻辑，并在被拦截时给出明确提示；如需自动签到需真浏览器/人工。

登录方式：cookie（环境变量 PCBETA_COOKIE）
cookie 示例：jqCP_887f_saltkey=xxx; jqCP_887f_auth=yyy; ...
（前缀 jqCP_887f_ 为 Discuz 安装生成，若站点升级变化需同步更新）
"""

import os
import re
import requests
from datetime import datetime, timedelta, timezone

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

APPLY_URL = "https://i.pcbeta.com/home.php?mod=task&do=apply&id=149"
DRAW_URL = "https://i.pcbeta.com/home.php?mod=task&do=draw&id=149"
CREDIT_URL = "https://i.pcbeta.com/home.php?mod=spacecp&ac=credit"
TASK_NEW_URL = "https://i.pcbeta.com/home.php?mod=task&item=new"


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


def extract_pb(html_text):
    try:
        m = re.search(r"PB币[^\d]*?(\d+)", html_text)
        return m.group(1) if m else "未知"
    except Exception:
        return "未知"


def main():
    raw_cookie = os.getenv("PCBETA_COOKIE", "")
    if not raw_cookie:
        msg = "❌ 错误: 环境变量 PCBETA_COOKIE 配置缺失"
        print(msg)
        write_result("PCBETA签到 " + msg)
        return

    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Referer": "https://bbs.pcbeta.com/",
        "Cookie": raw_cookie,
    })

    # 先探测是否被反爬（滑块验证码）拦截
    try:
        probe = session.get(TASK_NEW_URL, timeout=20)
    except Exception as e:
        msg = f"❌ 请求失败: {e}"
        print(msg)
        write_result("PCBETA签到 " + msg)
        return

    if probe.status_code == 403 or "异常请求验证" in probe.text:
        msg = ("❌ pcbeta 触发反爬「异常请求验证」(滑块验证码+问卷)，"
               "纯HTTP无法自动通过，需在浏览器/住宅IP环境人工处理")
        print(msg)
        write_result("PCBETA签到 " + msg)
        return

    log = []

    # 1) 申请每日打卡任务（id=149）
    try:
        apply_res = session.get(APPLY_URL, timeout=20).text
    except Exception as e:
        msg = f"❌ 申请任务失败: {e}"
        print(msg)
        write_result("PCBETA签到 " + msg)
        return

    # 2) 领取/完成任务
    try:
        draw_res = session.get(DRAW_URL, timeout=20).text
    except Exception as e:
        msg = f"❌ 完成任务失败: {e}"
        print(msg)
        write_result("PCBETA签到 " + msg)
        return

    if "成功完成" in draw_res or "恭喜您" in draw_res:
        result = "✅ 签到成功，PB币+1"
    elif "不是进行中" in draw_res or "已申请过" in apply_res:
        result = "✅ 今日已签到（重复）"
    else:
        result = "❌ 签到失败，请检查 cookie 是否过期"
    log.append(result)

    try:
        credit_res = session.get(CREDIT_URL, timeout=20).text
        pb = extract_pb(credit_res)
        nick_m = re.search(r'访问我的空间">(.+?)<', credit_res)
        nick = nick_m.group(1) if nick_m else "未知用户"
        asset_line = f"👤 {nick} | 💰 PB币: {pb}"
    except Exception as e:
        asset_line = f"⚠️ 资产获取失败: {e}"
    log.append(asset_line)

    final = "PCBETA签到 " + " | ".join(log)
    print(final)
    write_result(final)


if __name__ == "__main__":
    main()
