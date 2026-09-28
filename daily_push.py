# -*- coding: utf-8 -*-
"""汇总当日签到结果并推送到 PushPlus。

签到脚本（enshan.py / fnclub.py / znds.py / hifiti.js）将结果
追加写入本地文件 checkin_results.txt（已被 .gitignore 忽略，不入库），
本脚本读取该文件内容推送 PushPlus，推送成功后清空，供下一轮使用。
"""
import os
import requests
from datetime import datetime, timedelta, timezone

# ===================== 配置 =====================
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN")
RESULTS_FILE = "checkin_results.txt"

def beijing_time():
    beijing_tz = timezone(timedelta(hours=8))
    return datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")

def read_results():
    if not os.path.exists(RESULTS_FILE):
        return ""
    try:
        with open(RESULTS_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"❌ 读取结果文件失败：{e}")
        return ""

def clear_results():
    try:
        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            f.write("")
        print("✅ 结果文件已清空，等待下一轮签到")
    except Exception as e:
        print(f"❌ 清空失败：{e}")

def push(content):
    if not PUSHPLUS_TOKEN:
        print("❌ 未配置 PUSHPLUS_TOKEN")
        return False

    if not content.strip():
        print("ℹ️ 无签到结果，跳过推送")
        return True

    url = "http://www.pushplus.plus/send"
    data = {
        "token": PUSHPLUS_TOKEN,
        "title": f"签到日志汇总 {beijing_time()}",
        "content": content.replace("\n", "<br>"),
        "template": "html"
    }

    try:
        r = requests.post(url, json=data, timeout=20)
        if r.status_code == 200 and r.json().get("code") == 200:
            print("✅ 推送成功")
            return True
        else:
            print(f"❌ 推送失败：{r.text}")
            return False
    except Exception as e:
        print(f"❌ 推送异常：{e}")
        return False

def main():
    print("=== 每日签到日志推送任务 ===")
    content = read_results()
    blocks = [b.strip() for b in content.split("\n\n") if b.strip()]
    if blocks:
        # 打印汇总条目概览，便于从 Actions 日志直接核对本轮覆盖了哪些站点
        print(f"ℹ️ 本次汇总 {len(blocks)} 条结果：")
        for b in blocks:
            first = b.splitlines()[0]
            print("   - " + (first[:70] + "…" if len(first) > 70 else first))
    else:
        print("ℹ️ 未读取到签到结果")
    if push(content):
        clear_results()

if __name__ == "__main__":
    main()
