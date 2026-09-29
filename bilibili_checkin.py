#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""哔哩哔哩每日任务（GitHub Actions 定时运行版）

并入 my-daily-checkin：由 sign_all.yml 统一调度，结果追加写入 checkin_results.txt，
交给 daily_push.py 汇总后经 PushPlus 推送。

参考源：dangks/bilibili_checkin（MIT License，Copyright (c) 2025 Dangks）。

本文件在该项目基础上按本仓库约定做了以下适配：

1. **去掉 loguru 依赖**，改用 print —— 与仓库其他脚本一致，Actions 只需 pip install requests；
2. **去掉独立推送**（源的 main.py + push.py 会自行发 PushPlus），改由 daily_push.py
   统一汇总推送一次，避免每天收到两条推送；
3. **日志脱敏**（源存在的问题，本文件已消除）：
   - 源的 push.py 把 B 站昵称（uname）原样拼进推送正文，经 PushPlus（第三方）出网；
     本文件不打印昵称，仅输出 UID 的哈希指纹用于跨运行比对账号是否变更；
   - 源的 mask_string()/mask_uid()（定义在 main.py）保留首字符/前 2 位，属弱脱敏；
     本文件统一改用 logsafe.mask_uid()（sha256 前 8 位 + 长度，不可反推）；
   - Cookie 一律不打印，任何片段（含 bili_jct）都不进日志。
4. **推送端点统一 https**（源的 push.py 使用明文 http，本仓库已全部改 https）；
5. 所有 HTTP 请求补 timeout，来源版本未设超时，网络异常时会长时间挂住；
6. 退出码对齐本仓库约定；
7. **默认任务按本仓库风格收紧**为 add_coin,share_video,watch_video；
   漫画签到 / 银瓜子兑换 / 应援团签到功能代码全部保留，在 Secrets 里配
   TASK_CONFIG 即可开启，无需改代码。

退出码：0 成功/已签 | 2 配置缺失 | 4 网络失败 | 5 API 拒绝（含 Cookie 失效）
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

from logsafe import mask_uid, redact

RESULTS_FILE = "checkin_results.txt"   # 汇总结果文件（由 daily_push.py 读取后推送 PushPlus）
SITE_NAME = "哔哩哔哩"
TIMEOUT = 20                           # 单次请求超时（秒）；源版本未设超时
UA = ("Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/86.0.4240.198 Safari/537.36")

# 视频列表各来源均失败时的兜底（B 站官方公告视频，长期存在）
FALLBACK_BVID = "BV1GJ411x7h7"

# 任务名 -> 推送展示名
TASK_LABELS = {
    "live_sign": "直播签到",
    "manga_sign": "漫画签到",
    "share_video": "分享视频",
    "add_coin": "投币",
    "watch_video": "观看视频",
    "silver2coin": "银瓜子兑换",
    "link_sign": "应援团签到",
}

# 默认任务：投币 + 分享 + 观看（其余任务默认关闭，改 TASK_CONFIG 即可开启）
DEFAULT_TASKS = "add_coin,share_video,watch_video"

# 视为「非失败」的提示语，不计入失败判定
IGNORE_FAIL_KEYWORDS = ["未配置", "跳过", "已下线", "已签", "无应援团"]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ────────────────────────── 结果写入 ──────────────────────────

def write_result(content):
    """把本轮结果追加写入 checkin_results.txt（与 enshan.py 等脚本同格式，
    状态符号由调用方置于内容最前，即「时间 - ✅ 站点名 内容」）。
    该文件已被 .gitignore 忽略，不入库。"""
    try:
        beijing = timezone(timedelta(hours=8))
        now = datetime.now(beijing).strftime("%Y-%m-%d %H:%M:%S")
        with open(RESULTS_FILE, "a", encoding="utf-8") as f:
            f.write(f"{now} - {content}\n\n")
        print("[信息] 签到结果已追加写入 checkin_results.txt")
    except Exception as e:
        print(f"[错误] 写入 checkin_results.txt 失败: {redact(e)}")


# ────────────────────────── B 站 API 封装 ──────────────────────────

class BilibiliTask:
    """B 站任务封装。参考源 dangks/bilibili_checkin（MIT）。

    说明：所有方法返回 (bool, str) 二元组，str 为可直接进推送的简短描述。
    """

    def __init__(self, cookie):
        self.cookie = cookie
        self.headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.bilibili.com/",
            "Cookie": cookie,
        }
        self.csrf = self._get_csrf()

    def _get_csrf(self):
        """从 Cookie 中取 bili_jct 作为 csrf 参数（B 站写操作必需）。"""
        for item in self.cookie.split(";"):
            if item.strip().startswith("bili_jct"):
                # split('=', 1)：Cookie 值本身可能含 '='，不能按全部分隔符切
                return item.split("=", 1)[1]
        return None

    # ---- HTTP 基础（统一带超时与异常收敛）----

    def _get(self, url):
        return requests.get(url, headers=self.headers, timeout=TIMEOUT)

    def _post(self, url, data, extra_headers=None):
        headers = dict(self.headers)
        if extra_headers:
            headers.update(extra_headers)
        return requests.post(url, headers=headers, data=data, timeout=TIMEOUT)

    # ---- 账号与视频源 ----

    def get_user_info(self):
        """获取登录用户信息；Cookie 失效时返回 None（调用方据此判定退出码）。

        注意：**网络异常不在此吞掉**，直接向上抛出 —— 否则网络抖动会被误判成
        「Cookie 失效」，导致退出码与提示信息都指错方向（调用方按 4/5 区分）。
        """
        url = "https://api.bilibili.com/x/web-interface/nav"
        data = self._get(url).json()
        if data.get("code") == 0:
            return data.get("data")
        print(f"[警告] 获取用户信息失败: {redact(data.get('message'))}")
        return None

    def get_dynamic_videos(self):
        """动态视频（回退来源）。

        注意：dynamic/region 接口已被 B 站废弃（返回 code=-404 "啥都木有"），
        仅作为回退来源保留，默认投币来源为 ranking。
        """
        url = "https://api.bilibili.com/x/web-interface/dynamic/region?ps=5&rid=1"
        try:
            data = self._get(url).json()
            if data.get("code") == 0:
                return [v["bvid"] for v in data.get("data", {}).get("archives", [])]
            print(f"[警告] 动态视频接口返回非 0: code={data.get('code')}"
                  f"（该接口疑似已废弃）")
            return []
        except Exception as e:
            print(f"[错误] 请求动态视频接口异常: {redact(e)}")
            return []

    def get_ranking_videos(self):
        """排行榜视频（默认投币来源，实测可用）。"""
        url = "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all"
        try:
            data = self._get(url).json()
            if data.get("code") == 0:
                return [v["bvid"] for v in data.get("data", {}).get("list", [])]
            print(f"[警告] 排行榜视频接口返回非 0: code={data.get('code')}")
            return []
        except Exception as e:
            print(f"[错误] 请求排行榜视频接口异常: {redact(e)}")
            return []

    def check_video_coin_status(self, bvid):
        """判断该视频今日是否已被本账号投过币（用于去重，避免重复投报错）。"""
        url = f"https://api.bilibili.com/x/web-interface/archive/coins?bvid={bvid}"
        try:
            data = self._get(url).json()
            if data.get("code") == 0:
                return data.get("data", {}).get("multiply", 0) > 0
            return False
        except Exception:
            return False

    # ---- 各项任务 ----

    def add_coin(self, bvid, num=1, select_like=1):
        """投币（消耗硬币）。select_like=1 表示同时点赞。"""
        if not self.csrf:
            return False, "Bili_jct(csrf) 未找到"
        url = "https://api.bilibili.com/x/web-interface/coin/add"
        data = {"bvid": bvid, "multiply": num, "select_like": select_like, "csrf": self.csrf}
        try:
            r = self._post(url, data).json()
            if r.get("code") == 0:
                return True, "投币成功"
            return False, str(r.get("message") or "投币失败")
        except Exception as e:
            return False, redact(e)

    def share_video(self, bvid):
        """分享视频（得经验，无消耗）。"""
        if not self.csrf:
            return False, "Bili_jct(csrf) 未找到"
        url = "https://api.bilibili.com/x/web-interface/share/add"
        data = {"bvid": bvid, "csrf": self.csrf}
        try:
            r = self._post(url, data).json()
            if r.get("code") == 0:
                return True, "分享成功"
            return False, str(r.get("message") or "分享失败")
        except Exception as e:
            return False, redact(e)

    def watch_video(self, bvid):
        """上报观看进度（得经验，无消耗）。"""
        url = "https://api.bilibili.com/x/click-interface/web/heartbeat"
        data = {"bvid": bvid, "played_time": 30, "csrf": self.csrf}
        try:
            r = self._post(url, data).json()
            if r.get("code") == 0:
                return True, "观看成功"
            return False, str(r.get("message") or "观看失败")
        except Exception as e:
            return False, redact(e)

    def live_sign(self):
        """直播区签到（默认关闭）。"""
        url = "https://api.live.bilibili.com/xlive/web-ucenter/v1/sign/DoSign"
        try:
            r = self._get(url).json()
            if r.get("code") == 0:
                return True, "直播签到成功"
            return False, str(r.get("message") or "直播签到失败")
        except Exception as e:
            return False, redact(e)

    def manga_sign(self):
        """漫画签到（默认关闭）。接口接受 form-urlencoded，platform 取 android。"""
        url = "https://manga.bilibili.com/twirp/activity.v1.Activity/ClockIn"
        try:
            r = self._post(url, {"platform": "android"}).json()
            code = r.get("code")
            msg = r.get("msg") or r.get("message") or ""
            # 成功 code=0；今日已签（code=1 / invalid_argument 且提示重复签到）视为成功
            if code == 0 or "重复签到" in msg or code == "invalid_argument":
                return True, "漫画签到成功"
            print(f"[警告] 漫画签到接口返回: code={code}")
            return False, str(msg or "漫画签到失败")
        except Exception as e:
            return False, redact(e)

    def silver2coin(self):
        """银瓜子兑换硬币（默认关闭）。每日最多 1 枚，需账号有银瓜子。"""
        if not self.csrf:
            return False, "Bili_jct(csrf) 未找到"
        url = "https://api.live.bilibili.com/pay/v1/Exchange/silver2coin"
        data = {"csrf": self.csrf, "csrf_token": self.csrf}
        try:
            r = self._post(url, data).json()
            code = r.get("code")
            msg = r.get("message") or ""
            if code == 0:
                return True, "银瓜子兑换成功"
            # 无银瓜子 / 今日已兑换：非失败，视为跳过
            if any(k in msg for k in ("不足", "已经兑换", "已兑换", "今天")):
                return False, "银瓜子不足或今日已兑换，跳过"
            print(f"[警告] 银瓜子兑换返回: code={code}")
            return False, str(msg or "银瓜子兑换失败")
        except Exception as e:
            return False, redact(e)

    def get_link_groups(self):
        """获取已加入的应援团列表（默认关闭）。

        注意：粉丝勋章本身无独立签到接口，可每日签到的是应援团（link_setting/sign_in）。
        """
        url = "https://api.vc.bilibili.com/link_group/v1/member/my_groups"
        try:
            r = self._get(url).json()
            if r.get("code") == 0:
                groups = r.get("data", {}).get("groups", [])
                if isinstance(groups, dict):
                    groups = groups.get("list", [])
                return [(g.get("group_id"), g.get("owner_id")) for g in groups
                        if g.get("group_id") and g.get("owner_id")]
            print(f"[警告] 应援团列表返回非 0: code={r.get('code')}")
            return []
        except Exception as e:
            print(f"[错误] 请求应援团列表异常: {redact(e)}")
            return []

    def link_sign(self, group_id, owner_id):
        """应援团签到（默认关闭）。"""
        if not self.csrf:
            return False, "Bili_jct(csrf) 未找到"
        url = "https://api.vc.bilibili.com/link_setting/v1/link_setting/sign_in"
        data = {"group_id": group_id, "owner_id": owner_id, "csrf": self.csrf}
        try:
            r = self._post(url, data, {"Referer": "https://live.bilibili.com/",
                                       "Origin": "https://live.bilibili.com"}).json()
            code = r.get("code")
            msg = r.get("message") or ""
            if code == 0:
                return True, "应援团签到成功"
            if any(k in msg for k in ("重复", "已", "已经")):
                return True, "应援团今日已签"
            print(f"[警告] 应援团签到返回: code={code}")
            return False, str(msg or "应援团签到失败")
        except Exception as e:
            return False, redact(e)


# ────────────────────────── 任务编排 ──────────────────────────

def _int_env(name, default):
    """读取整数型环境变量，非法值回落默认（配置错误不应让脚本崩掉）。"""
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def execute_coin_task(bili, user_info, cfg):
    """投币：按配置数量投币，带来源回退与已投去重。"""
    coins_to_add = cfg["COIN_ADD_NUM"]
    if coins_to_add <= 0:
        return True, "投币 0 枚（配置为 0，跳过）"

    balance = int(user_info.get("money", 0) or 0)
    if balance < 1:
        return True, f"投币跳过（硬币不足 {balance}）"

    coins_to_add = min(coins_to_add, balance, 5)   # B 站单日上限 5 枚

    source = cfg["COIN_VIDEO_SOURCE"]
    if source == "ranking":
        videos = bili.get_ranking_videos()
        print("[信息] 投币来源：排行榜")
    else:
        videos = bili.get_dynamic_videos()
        print("[信息] 投币来源：动态")

    # 回退：主来源取不到换另一来源，再取不到用兜底视频
    if not videos:
        print("[警告] 主来源未取到视频，切换备用来源")
        videos = bili.get_dynamic_videos() if source == "ranking" else bili.get_ranking_videos()
    if not videos:
        print("[警告] 所有来源均未取到视频，使用兜底视频")
        videos = [FALLBACK_BVID]

    added = 0
    for bvid in videos:
        if added >= coins_to_add:
            break
        # 跳过今日已投过的视频，避免对同一视频重复投报「超过投币上限」噪音
        if bili.check_video_coin_status(bvid):
            print(f"[信息] 视频 {bvid} 今日已投过，跳过")
            continue
        ok, msg = bili.add_coin(bvid, 1, cfg["COIN_SELECT_LIKE"])
        if ok:
            added += 1
            print(f"[信息] 为视频 {bvid} 投币成功")
        elif "已达到" in msg:
            print("[警告] 今日投币上限已满，终止投币")
            added = coins_to_add
            break
        else:
            print(f"[警告] 为视频 {bvid} 投币失败: {redact(msg)}")
            if "硬币不足" in msg:
                break

    return True, f"投币 {added} 枚"


def run_tasks(bili, cfg):
    """执行配置中的任务，返回 (results, user_info)。

    results 为有序列表 [(任务名, 是否成功, 简短描述)]，描述可直接进推送。
    返回 (None, None) 表示 Cookie 失效或网络异常，调用方据此判定退出码。
    """
    tasks_to_run = [t.strip() for t in cfg["TASK_CONFIG"].split(",") if t.strip()]
    if not tasks_to_run:
        tasks_to_run = DEFAULT_TASKS.split(",")

    user_info = bili.get_user_info()
    if not user_info:
        return None, None

    # 仅输出 UID 指纹：昵称属可识别信息，一律不进日志（源版曾原样输出）
    print(f"[信息] 账号 uid: {mask_uid(user_info.get('mid'))}")
    print(f"[信息] 待执行任务: {', '.join(tasks_to_run)}")

    results = []

    # 分享/观看目标视频：优先排行榜（实测可用），回退动态，再回退兜底
    videos = bili.get_ranking_videos() or bili.get_dynamic_videos()
    bvid = videos[0] if videos else FALLBACK_BVID

    if "share_video" in tasks_to_run:
        ok, msg = bili.share_video(bvid)
        results.append(("分享视频", ok, "分享成功" if ok else f"分享失败({msg})"))

    if "live_sign" in tasks_to_run:
        ok, msg = bili.live_sign()
        results.append(("直播签到", ok, msg))

    if "manga_sign" in tasks_to_run:
        ok, msg = bili.manga_sign()
        results.append(("漫画签到", ok, msg))

    if "add_coin" in tasks_to_run:
        ok, msg = execute_coin_task(bili, user_info, cfg)
        results.append(("投币", ok, msg))

    if "silver2coin" in tasks_to_run:
        ok, msg = bili.silver2coin()
        results.append(("银瓜子兑换", ok, msg))

    if "link_sign" in tasks_to_run:
        groups = bili.get_link_groups()
        if not groups:
            results.append(("应援团签到", True, "无应援团，跳过"))
        else:
            done = 0
            for gid, oid in groups:
                ok, msg = bili.link_sign(gid, oid)
                if ok:
                    done += 1
                else:
                    print(f"[警告] 应援团签到失败: {redact(msg)}")
            results.append(("应援团签到", done > 0, f"已签 {done}/{len(groups)} 个"))

    if "watch_video" in tasks_to_run:
        ok, msg = bili.watch_video(bvid)
        results.append(("观看视频", ok, "观看成功" if ok else f"观看失败({msg})"))

    return results, user_info


def summarize(results):
    """把子任务结果压成一行描述，返回 (整体是否成功, 描述文本)。

    失败判定跳过 IGNORE_FAIL_KEYWORDS 命中的「非失败」项（如跳过、已签）。
    """
    all_ok = True
    parts = []
    for name, ok, msg in results:
        if not ok and not any(k in msg for k in IGNORE_FAIL_KEYWORDS):
            all_ok = False
        parts.append(msg if msg else name)
    return all_ok, " · ".join(parts)


# ────────────────────────── 入口 ──────────────────────────

def main():
    print("=" * 56)
    print("哔哩哔哩每日任务  ", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 56)

    cookie = (os.environ.get("BILIBILI_COOKIE") or "").strip()
    if not cookie:
        print("[失败] 缺少凭据：需环境变量 BILIBILI_COOKIE（Cookie 全文）。")
        write_result(f"❌ {SITE_NAME} 任务失败：缺少凭据（BILIBILI_COOKIE）")
        return 2

    cfg = {
        "TASK_CONFIG": os.environ.get("TASK_CONFIG") or DEFAULT_TASKS,
        "COIN_ADD_NUM": _int_env("COIN_ADD_NUM", 1),
        "COIN_SELECT_LIKE": _int_env("COIN_SELECT_LIKE", 1),
        "COIN_VIDEO_SOURCE": os.environ.get("COIN_VIDEO_SOURCE") or "ranking",
    }
    # 不打印 cookie 本身，仅报长度，便于确认 Secret 是否为空串
    print(f"[信息] Cookie 已注入（长度 {len(cookie)}）")

    try:
        results, user_info = run_tasks(BilibiliTask(cookie), cfg)
    except requests.RequestException as e:
        print(f"[失败] 网络错误: {redact(e)}")
        write_result(f"❌ {SITE_NAME} 任务失败：网络错误")
        return 4
    except Exception as e:
        print(f"[失败] 未预期异常: {redact(e)}")
        write_result(f"❌ {SITE_NAME} 任务失败：{redact(e)[:120]}")
        return 4

    if results is None:
        print("[失败] Cookie 失效或账号信息获取失败，请更新 Secret BILIBILI_COOKIE。")
        write_result(f"❌ {SITE_NAME} 任务失败：Cookie 失效或账号信息获取失败")
        return 5

    for name, ok, msg in results:
        print(f"[{'成功' if ok else '失败'}] {name}: {msg}")

    all_ok, detail = summarize(results)
    print(f"[结果] {detail}")
    write_result(f"{'✅' if all_ok else '❌'} {SITE_NAME} {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
