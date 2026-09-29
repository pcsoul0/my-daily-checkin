# -*- coding: utf-8 -*-
"""
雨云（rainyun.com）每日签到 —— API Key 纯 HTTP 方案

设计取舍：
- 不走浏览器、不存账号密码。凭据用雨云后台「用户中心 → API 密钥」生成的 API Key
  （可随时吊销），通过 `x-api-key` 请求头认证，比社区常见的账密登录方案少一层
  登录风控面，也不会在仓库里留账号密码。
- 不破解验证码。

接口（2026-09-29 实测「端点存在 + 认证方式」，未持有效凭据，故未做业务级实测）：
    GET  /user/                → 积分余额（data.Points）
    GET  /user/reward/tasks    → 积分任务列表（尝试解析「每日签到」的领取状态）
    POST /user/reward/tasks    → 领取任务奖励，body {"task_name":"每日签到","verifyCode":""}
    探测结论：以无效 key 请求 POST /user/reward/tasks 返回
    {"code":30039,"message":"密钥认证错误或已失效"} / HTTP 403，
    即端点在服务端存在且确实走 x-api-key 认证（非 404、非 cookie 专属）。

⚠️ 已知约束（务必知悉，出问题先看这里）：
    雨云「每日签到」在风控命中时会弹腾讯滑块验证码（前端 iframe id: tcaptcha_iframe_dy）。
    社区两个流派对它的处理都是回避而非破解：
      · henjiu123/Rainyun-QingLong —— 浏览器自动化 + ddddocr/打码，重且不稳；
      · 345yun 下载量最高的流行版本 —— 直接判定「需验证码，本次跳过」。
    本脚本采用同样克制的策略：一旦接口提示需要验证码，就如实写入结果并结束，
    不接打码服务、不做任何绕过。因此**存在某几天签到不上的可能**，
    表现为推送里出现「⚠️ 雨云：需人工验证码」。这是预期行为，不是 Bug。

凭据：
    RAINYUN_API_KEY —— 雨云后台「用户中心 → API 密钥」创建（必填）

退出码（对齐本仓库 workbuddy / ima / bilibili 的约定）：
    0 成功或今日已签；2 配置缺失；4 网络失败；5 API 拒绝（含密钥失效、需验证码）
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from logsafe import mask_secret, redact, redact_obj

API_BASE = "https://api.v2.rainyun.com"
TASK_NAME = "每日签到"
TIMEOUT = 20
MAX_RETRIES = 3
RETRY_DELAY = 3

EXIT_OK, EXIT_CONFIG, EXIT_NETWORK, EXIT_API = 0, 2, 4, 5

# 幂等信号：接口把「今天已经领过了」当错误码抛出时，按成功处理。
_ALREADY_DONE_HINT = ("已领取", "已经领取", "已签到", "已经签到", "重复", "already")
# 验证码信号：命中即判定需人工，不做任何绕过。
_CAPTCHA_HINT = ("验证码", "captcha", "verify", "滑块")


def beijing_now():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def write_result(content):
    """结果追加写入 checkin_results.txt（与其它签到脚本同一约定，交由 daily_push.py 汇总）。"""
    try:
        with open("checkin_results.txt", "a", encoding="utf-8") as f:
            f.write(f"{beijing_now()} - {content}\n\n")
        print("[INFO] 签到结果已追加写入 checkin_results.txt")
    except Exception as e:
        print(f"[ERROR] 写入 checkin_results.txt 失败: {e}")


def finish(content, code):
    """统一出口：打印 + 落盘 + 返回退出码。"""
    print("=== 签到结果 ===")
    print(content)
    write_result(content)
    return code


class RainyunAPI:
    """雨云 API 客户端（x-api-key 认证）。"""

    def __init__(self, api_key):
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            # 与网页端同源，降低被判定为异常客户端概率
            "Origin": "https://app.rainyun.com",
            "Referer": "https://app.rainyun.com/",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
        })
        # 最近一次请求是否因网络层原因彻底失败（供退出码判定）
        self.network_failed = False

    def request(self, method, path, payload=None):
        """返回 (http_status, body_dict)。网络层瞬时错误按固定间隔重试。"""
        url = f"{API_BASE}{path}"
        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if method == "GET":
                    resp = self.session.get(url, timeout=TIMEOUT)
                else:
                    resp = self.session.post(url, json=payload, timeout=TIMEOUT)
                try:
                    return resp.status_code, resp.json()
                except ValueError:
                    return resp.status_code, {
                        "code": -1,
                        "message": "响应不是有效 JSON",
                        "raw": redact(resp.text)[:200],
                    }
            except requests.RequestException as e:
                last_err = e
                if attempt < MAX_RETRIES:
                    print(f"   ⚠️ 请求失败（第 {attempt} 次）：{redact(str(e))[:120]}，"
                          f"{RETRY_DELAY}s 后重试")
                    time.sleep(RETRY_DELAY)

        self.network_failed = True
        return 0, {"code": -1, "message": f"网络请求失败：{redact(str(last_err))[:120]}"}

    def get_points(self):
        """积分余额，失败返回 None。"""
        _, body = self.request("GET", "/user/")
        if body.get("code") == 200 and isinstance(body.get("data"), dict):
            return body["data"].get("Points")
        return None

    def get_checkin_status(self):
        """尝试读「每日签到」当前状态。接口结构变动时返回 None，不影响后续签到。"""
        _, body = self.request("GET", "/user/reward/tasks")
        if body.get("code") != 200:
            return None
        tasks = body.get("data")
        if not isinstance(tasks, list):
            return None
        for item in tasks:
            if isinstance(item, dict) and item.get("Name") == TASK_NAME:
                # 雨云任务状态：0 未完成 / 1 可领取 / 2 已领取
                return item.get("Status")
        return None

    def do_checkin(self):
        """领取「每日签到」奖励。返回 (http_status, body)。"""
        return self.request("POST", "/user/reward/tasks",
                            {"task_name": TASK_NAME, "verifyCode": ""})


def classify(http_status, body):
    """把接口返回归类。返回 (状态key, 可读消息)。"""
    code = body.get("code")
    message = str(body.get("message") or "").strip()
    text = f"{message} {body.get('raw', '')}".lower()

    if code == 200:
        return "ok", message or "领取成功"
    if code == 30039 or http_status == 403:
        return "auth", message or "密钥认证错误或已失效"
    if any(k in message for k in _CAPTCHA_HINT) or any(k in text for k in ("captcha", "verify")):
        return "captcha", message or "需要验证码"
    if any(k in message for k in _ALREADY_DONE_HINT):
        return "done", message or "今日已领取"
    return "fail", message or "未知错误"


def run():
    api_key = (os.getenv("RAINYUN_API_KEY") or "").strip()
    if not api_key:
        return finish("❌ 雨云签到失败：环境变量 RAINYUN_API_KEY 缺失"
                      "（雨云后台「用户中心 → API 密钥」创建后填入 Secrets）", EXIT_CONFIG)

    print("=== 开始执行雨云每日签到（API Key 方案）===")
    print(f"🔑 API Key：{mask_secret(api_key)}")

    api = RainyunAPI(api_key)

    try:
        points_before = api.get_points()
        if points_before is not None:
            print(f"💰 签到前积分：{points_before}")

        status = api.get_checkin_status()
        if status is not None:
            print(f"📌 任务状态：Status={status}（0 未完成 / 1 可领取 / 2 已领取）")

        print(f"🚀 正在领取「{TASK_NAME}」...")
        http_status, body = api.do_checkin()
        print(f"📥 接口返回（HTTP {http_status}）：{redact_obj(body, 400)}")

        state, message = classify(http_status, body)

        if state == "auth":
            return finish("❌ 雨云签到失败：API Key 无效或已失效（code 30039）。"
                          "请到雨云后台重新生成密钥并更新 GitHub Secret「RAINYUN_API_KEY」",
                          EXIT_API)
        if state == "captcha":
            return finish(f"⚠️ 雨云：需人工验证码，本次跳过（接口提示：{message}）。"
                          "脚本不做验证码绕过，可手动登录雨云领取今日签到", EXIT_API)
        if state == "fail":
            code = EXIT_NETWORK if api.network_failed else EXIT_API
            return finish(f"❌ 雨云签到失败：{message}", code)

        # ok / done 两种都算签到已达成，取积分增量作为佐证
        time.sleep(2)
        points_after = api.get_points()
        delta = ""
        if isinstance(points_before, int) and isinstance(points_after, int):
            delta = f"（本次 +{points_after - points_before}）"
        points_txt = points_after if points_after is not None else "未知"

        label = "签到成功" if state == "ok" else "今日已签到"
        return finish(f"✅ 雨云每日签到{label} | 积分：{points_txt}{delta}", EXIT_OK)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return finish(f"❌ 雨云脚本运行出错：{redact(str(e))[:200]}", EXIT_API)


if __name__ == "__main__":
    sys.exit(run())
