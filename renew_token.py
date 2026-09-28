#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 签到 token 续期工具

背景：新版 WorkBuddy 客户端的正式会话文件（workbuddy-desktop.info）中
accessToken/refreshToken 已改为 $wbEncrypted 加密信封存储，无法直接读取；
但客户端轮换登录态时会留下带时间戳的 *.info 备份文件，其中仍保存明文 JWT。
本脚本扫描这些备份文件，提取最新的有效 accessToken。

用法（在本机运行）：
  python renew_token.py                # 仅显示提取结果（脱敏，不发网络请求）
  python renew_token.py --secret       # 提取后直接写入 GitHub Secrets（需已 gh auth login）
  python renew_token.py --out token.info   # 提取后写入本地文件（JSON: token + uid，勿提交）
  python renew_token.py --repo your/name   # 指定仓库（默认 pcsoul0/my-daily-checkin）
"""
import argparse
import base64
import datetime
import glob
import json
import os
import subprocess
import sys

DEFAULT_REPO = "pcsoul0/my-daily-checkin"
AUTH_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                        "CodeBuddyExtension", "Data", "Public", "auth")


def jwt_exp(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        p = json.loads(base64.urlsafe_b64decode(payload))
        return p.get("exp"), p.get("sub")
    except Exception:
        return None, None


def collect_candidates():
    """扫描 auth 目录全部 workbuddy-desktop*.info，收集明文 JWT 候选。"""
    cands = []
    for p in glob.glob(os.path.join(AUTH_DIR, "workbuddy-desktop*.info")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        auth = d.get("auth") or {}
        tok = auth.get("accessToken")
        uid = (d.get("account") or {}).get("uid") or ""
        domain = auth.get("domain") or "www.workbuddy.cn"
        if isinstance(tok, dict):
            state = "加密信封($wbEncrypted)，跳过"
        elif isinstance(tok, str) and tok.count(".") == 2:
            exp, sub = jwt_exp(tok)
            state = f"明文 JWT, exp={datetime.datetime.fromtimestamp(exp)}" if exp else "明文 JWT, exp 未知"
            cands.append({"path": p, "token": tok, "uid": uid,
                          "domain": domain, "exp": exp or 0, "sub": sub,
                          "state": state})
        else:
            state = "无 accessToken，跳过"
        print(f"[扫描] {os.path.basename(p)}  ->  {state}")
    return cands


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 签到 token 续期")
    ap.add_argument("--secret", action="store_true",
                    help="写入 GitHub Secrets（WB_TOKEN / WB_UID，需 gh 已登录）")
    ap.add_argument("--out", metavar="FILE", help="写入本地 JSON 文件（token + uid）")
    ap.add_argument("--repo", default=DEFAULT_REPO, help=f"GitHub 仓库（默认 {DEFAULT_REPO}）")
    args = ap.parse_args()

    if not os.path.isdir(AUTH_DIR):
        print(f"[失败] 未找到 WorkBuddy 数据目录: {AUTH_DIR}")
        return 2
    print(f"[扫描] 目录: {AUTH_DIR}")

    cands = collect_candidates()
    valid = [c for c in cands if c["exp"] > datetime.datetime.now().timestamp()]
    if not valid:
        print("\n[失败] 没有有效期内的明文 token。")
        print("提示：先在本机打开一次 WorkBuddy 客户端（触发登录态刷新并生成新备份），再运行本脚本。")
        return 3

    valid.sort(key=lambda c: c["exp"], reverse=True)
    best = valid[0]
    print(f"\n[选用] {os.path.basename(best['path'])}")
    print(f"  token: {best['token'][:6]}...{best['token'][-6:]} (len={len(best['token'])})")
    print(f"  有效期至: {datetime.datetime.fromtimestamp(best['exp'])}")
    print(f"  uid: {best['uid'][:8]}...  domain: {best['domain']}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"token": best["token"], "uid": best["uid"],
                       "domain": best["domain"]}, f, ensure_ascii=False)
        print(f"[完成] 已写入 {args.out}（该文件已被 .gitignore 忽略，勿提交）")

    if args.secret:
        for name, value in (("WB_TOKEN", best["token"]), ("WB_UID", best["uid"])):
            r = subprocess.run(["gh", "secret", "set", name, "--repo", args.repo, "--body", value],
                               capture_output=True, text=True)
            if r.returncode == 0:
                print(f"[完成] GitHub Secret {name} 已更新（{args.repo}）")
            else:
                print(f"[失败] 写入 {name} 失败: {r.stderr.strip()}")
                return 4
        print("[提示] 可到 Actions 页面手动 Run workflow 立即验证。")

    if not args.out and not args.secret:
        print("\n[提示] 加 --secret 直接写入 GitHub Secrets，或 --out token.info 存本地。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
