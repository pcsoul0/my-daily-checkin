#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 每日积分签到（GitHub Actions 定时运行版）

并入 my-daily-checkin：由 sign_all.yml 统一调度（北京时间 00:09），
结果追加写入 checkin_results.txt，交给 daily_push.py 汇总推送 PushPlus。

设计要点（相对 wangmingdong/workbuddy-signin 的 workbuddy_checkin.py 的适配改动）：
1. 凭据来源改为环境变量优先（GitHub Secrets 注入），本地文件仅作调试兜底；
2. 删除 checkin-status 预检，直接调幂等的 daily-checkin（code=10001 = 今日已签），
   规避 today_checked_in 字段假阳性导致的漏签；
3. 瞬时网络错误（DNS/超时/5xx）自动重试 3 次；
4. 结果写入 GITHUB_STEP_SUMMARY，并追加写入 checkin_results.txt
   （由 daily_push.py 汇总后经 PushPlus 推送）；
5. 日志脱敏：token/uid 只输出哈希指纹，接口返回体与异常文本统一过滤
   requestId / UUID / 凭据字段 / 手机号（本仓库已公开，日志不得残留可关联信息）。

退出码：0 成功/已签 | 2 配置缺失 | 4 网络失败 | 5 API 拒绝（含 token 过期）
"""
import base64
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

from logsafe import mask_secret, mask_uid, redact

# 网关拒绝 Python-urllib 默认 UA（get-user-resource 会 403），统一带浏览器 UA
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

API_BASE = os.environ.get("WB_API_BASE", "https://copilot.tencent.com").rstrip("/")
DOMAIN = os.environ.get("WB_DOMAIN", "www.workbuddy.cn")
MAX_RETRY = 3          # 瞬时错误重试次数
RETRY_INTERVAL = 5     # 重试间隔秒数
RESULTS_FILE = "checkin_results.txt"   # 汇总结果文件（由 daily_push.py 读取后推送 PushPlus）

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def write_result(content):
    """把本轮结果追加写入 checkin_results.txt（与 enshan.py 等脚本同格式），
    由 daily_push.py 汇总推送 PushPlus。该文件已被 .gitignore 忽略，不入库。"""
    try:
        beijing = datetime.timezone(datetime.timedelta(hours=8))
        now = datetime.datetime.now(beijing).strftime("%Y-%m-%d %H:%M:%S")
        with open(RESULTS_FILE, "a", encoding="utf-8") as f:
            f.write(f"{now} - {content}\n\n")
        print("[信息] 签到结果已追加写入 checkin_results.txt")
    except Exception as e:
        print(f"[错误] 写入 checkin_results.txt 失败: {e}")


def load_creds():
    """凭据：环境变量 WB_TOKEN / WB_UID 优先；否则读 WB_TOKEN_FILE 或脚本旁 token.info。
    文件格式：JSON {"token": "...", "uid": "..."}（调试用，勿提交入库）。"""
    token = os.environ.get("WB_TOKEN", "").strip()
    uid = os.environ.get("WB_UID", "").strip()
    if token and uid:
        return token, uid, "环境变量"

    path = os.environ.get("WB_TOKEN_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "token.info")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        token = token or (d.get("token") or "").strip()
        uid = uid or (d.get("uid") or "").strip()
        if token and uid:
            return token, uid, f"文件 {path}"
    return None, None, None


def jwt_exp(token):
    """解码 JWT 过期时间（仅用于日志提示；非 JWT 格式则忽略）。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        p = json.loads(base64.urlsafe_b64decode(payload))
        if p.get("exp"):
            return datetime.datetime.fromtimestamp(p["exp"])
    except Exception:
        pass
    return None


def call_api(path, token, uid, body=None):
    """POST 请求，返回 (status, body_text)。

    注意：该网关用 HTTP 400 + body code=10001 表示"今日已签到"（实测），
    因此 4xx/5xx 不在此抛异常，交由上层读 body 业务码判断；
    仅网络层错误（DNS/超时/连接失败）向上抛出以触发重试。
    """
    req = urllib.request.Request(API_BASE + path, method="POST",
                                 data=json.dumps(body or {}).encode("utf-8"))
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-User-Id", uid)
    req.add_header("X-Domain", DOMAIN)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json, text/plain, */*")
    # 禁用环境代理（Actions runner 无代理；本机调试时避免误走系统代理）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def call_with_retry(path, token, uid, body=None):
    """仅对网络异常与 5xx 重试；4xx 属业务响应（如 400+code=10001），不重试。"""
    last = None
    for i in range(MAX_RETRY):
        try:
            s, b = call_api(path, token, uid, body)
            if s >= 500:
                last = RuntimeError(f"HTTP {s}: {redact(b)[:200]}")
            else:
                return s, b
        except (urllib.error.URLError, OSError) as e:
            last = e
        if i < MAX_RETRY - 1:
            print(f"[重试] 第{i + 1}次失败（{last}），{RETRY_INTERVAL}s 后重试...")
            time.sleep(RETRY_INTERVAL)
    raise last


def write_summary(ok, lines):
    """写入 GITHUB_STEP_SUMMARY（仅 Actions 环境存在该变量）。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("## WorkBuddy 签到结果\n\n")
            f.write(("✅ **成功**\n\n" if ok else "❌ **失败**\n\n"))
            f.write("```\n" + "\n".join(lines) + "\n```\n")
    except Exception:
        pass


def main():
    log = []
    p = lambda *a: (log.append(" ".join(str(x) for x in a)),
                    print(" ".join(str(x) for x in a)))

    p("=" * 56)
    p("WorkBuddy 每日签到  ", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    p("=" * 56)

    token, uid, src = load_creds()
    if not token or not uid:
        p("[失败] 缺少凭据：需环境变量 WB_TOKEN + WB_UID（或 token.info 文件）。")
        write_result("❌ WorkBuddy 签到失败：缺少凭据（WB_TOKEN / WB_UID）")
        write_summary(False, log)
        return 2
    p(f"[信息] 凭据来源: {src}")
    p(f"[信息] token: {mask_secret(token)}")
    p(f"[信息] uid: {mask_uid(uid)}  domain: {DOMAIN}  api: {API_BASE}")

    exp = jwt_exp(token)
    if exp:
        p(f"[信息] Token 有效期至: {exp.strftime('%Y-%m-%d %H:%M:%S')}")
        if exp < datetime.datetime.now():
            p("[失败] accessToken 已过期，请重新从浏览器抓取并更新 GitHub Secret WB_TOKEN。")
            write_result("❌ WorkBuddy 签到失败：accessToken 已过期（需运行 renew_token.py 续期）")
            write_summary(False, log)
            return 5

    # 直接调幂等的 daily-checkin（不做 checkin-status 预检，规避假阳性漏签）
    print("[动作] POST /v2/billing/meter/daily-checkin ...")
    try:
        s, b = call_with_retry("/v2/billing/meter/daily-checkin", token, uid)
    except Exception as e:
        p(f"[失败] 签到网络错误（已重试{MAX_RETRY}次）: {redact(e)}")
        write_result(f"❌ WorkBuddy 签到失败：网络错误（已重试 {MAX_RETRY} 次）")
        write_summary(False, log)
        return 4
    p(f"[返回] HTTP {s}: {redact(b)[:400]}")

    try:
        resp = json.loads(b)
    except Exception:
        resp = {}
    code = resp.get("code")
    data = resp.get("data") or {}

    if code == 0:
        p("[结果] 签到成功！✅")
        status_text = "签到成功"
    elif code == 10001:
        p("[结果] 今日已签到（网关以 HTTP 400 + code=10001 表示，属正常）。✅")
        status_text = "今日已签到"
    else:
        p(f"[失败] 签到被拒绝 code={code} msg={redact(resp.get('msg'))}")
        if s in (401, 403) or code in (401, 403):
            p("[提示] 401/403 通常为 token 失效：重新从浏览器抓 Bearer token 并更新 Secret。")
        write_result(f"❌ WorkBuddy 签到失败：API 拒绝 code={code}")
        write_summary(False, log)
        return 5

    # 成功后补查一次状态拿积分概览（best effort，失败不影响结果）
    credit = ""
    try:
        s3, b3 = call_with_retry("/v2/billing/meter/checkin-activity-status", token, uid)
        if s3 == 200:
            d = (json.loads(b3).get("data") or {})
            credit = (f"累计 {d.get('total_credits')} / 今日 {d.get('today_credit')} "
                      f"/ 连签 {d.get('streak_days')} 天")
            p(f"[积分] {credit}")
    except Exception as e:
        p(f"[提示] 状态查询失败（不影响签到结果）: {redact(e)}")

    write_result("✅ WorkBuddy " + status_text + (f" | {credit}" if credit else ""))
    write_summary(True, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
