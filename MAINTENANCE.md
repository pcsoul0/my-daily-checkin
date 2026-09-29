# my-daily-checkin 维护与二次开发说明

本文件是 [README](README.md) 的补充。README 只保留**脚本说明、运行流程、部署步骤**，
上游对照、凭据续期、站点实现细节与维护约定集中在这里。

## 目录

- [目录结构](#目录结构)
- [二次开发说明](#二次开发说明)
- [WorkBuddy token 续期](#workbuddy-token-续期)
- [ima 凭据抓取与续期](#ima-凭据抓取与续期)
- [雨云签到说明](#雨云签到说明)
- [维护备忘](#维护备忘)
- [日志安全](#日志安全)

---

## 目录结构

```
.
├── .github/workflows/
│   └── sign_all.yml        # 每日总调度（23:51 八站点串行 + PushPlus 汇总；支持手动勾选站点补跑）
├── .heartbeat              # 保活时间戳（工作流自动提交，约每 45 天 1 次，见「维护备忘」）
├── enshan.py               # 恩山论坛签到
├── fnclub.py               # 飞牛论坛签到（含 WAF 解算）
├── znds.py                 # 智能电视网签到
├── hifiti.js               # HIFITI 论坛签到（Node，多账号）
├── workbuddy_checkin.py    # WorkBuddy 积分签到
├── ima_checkin.py          # 腾讯 ima 每日登录领算力
├── bilibili_checkin.py     # 哔哩哔哩每日任务（投币 / 分享 / 观看）
├── rainyun_checkin.py      # 雨云每日签到（API Key，不绕验证码）
├── renew_token.py          # WorkBuddy token 一键续期（本机运行）
├── daily_push.py           # PushPlus 汇总推送
├── logsafe.py              # 日志脱敏工具（所有脚本共用，见「日志安全」）
└── tools/
    └── capture_ima.js      # ima 登录态抓取（本机一次性运行，需 playwright-core）
```

## 二次开发说明

本仓库部分脚本移植或参考自社区开源项目，在此致谢，并逐条说明本项目的适配改动，便于日后跟随上游更新时比对。

| 本仓库脚本 | 上游项目 | 关系 | 本项目的主要改动 |
|---|---|---|---|
| `hifiti.js` | [ewigl/hifini-auto-checkin](https://github.com/ewigl/hifini-auto-checkin) 的 `main.js` | 移植 | ① 新增 `fetchWithRetry`：网络层瞬时错误（`fetch failed` / ECONN / ETIMEDOUT / ENOTFOUND / EAI_AGAIN）与 HTTP 5xx 按指数退避重试 3 次，单次请求 20s 超时；② 「今天已经签过啦」由上游的精确相等改为 `includes` 包含匹配，兼容站点提示语前缀变动；③ 结果写入 `checkin_results.txt` 交 `daily_push.py` 统一推送，替代上游写入 `GITHUB_OUTPUT`；④ 日志脱敏，不再输出账号名 |
| `workbuddy_checkin.py` | [wangmingdong/workbuddy-signin](https://github.com/wangmingdong/workbuddy-signin) 的 `workbuddy_checkin.py` | 参考签到逻辑 | ① 凭据来源改为环境变量优先（GitHub Secrets 注入），本地 token 文件仅作调试兜底；② **删除 `checkin-status` 预检**，直接调幂等的 `daily-checkin`（`code=10001` 即今日已签），规避上游 `today_checked_in` 字段假阳性导致的漏签；③ 瞬时网络错误自动重试 3 次；④ 结果写入 `GITHUB_STEP_SUMMARY` 与 `checkin_results.txt`；⑤ 全程不打印 token 本体 |
| `enshan.py` | [Sitoi/dailycheckin](https://github.com/Sitoi/dailycheckin) | 思路参考 | 纯 HTTP 签到实现，自行提取 formhash 并解析积分 |
| `bilibili_checkin.py` | [dangks/bilibili_checkin](https://github.com/dangks/bilibili_checkin)（MIT） | 移植 | ① 去掉 `loguru` 依赖改用 `print`，Actions 端只需装 `requests`；② 去掉上游的独立 PushPlus 推送（`main.py` + `push.py`），改由 `daily_push.py` 统一汇总，避免每天收到两条推送；③ **日志脱敏**：上游 `push.py` 把 B 站昵称原样推送到第三方，本仓库不打印昵称、仅输出 UID 哈希指纹，并把上游 `mask_string()` / `mask_uid()` 的弱脱敏（保留首字符 / 前 2 位）统一改用 `logsafe`；④ 推送逻辑移除后，端点不再涉及上游的明文 `http://`；⑤ 所有 HTTP 请求补 20s 超时（上游未设，网络异常会长时间挂住）；⑥ 退出码对齐本仓库约定；⑦ 默认任务收紧为 `add_coin,share_video,watch_video`，漫画 / 银瓜子 / 应援团功能保留、按需用 `TASK_CONFIG` 开启 |

> ⚠️ 上游 `ewigl/hifini-auto-checkin` 仓库未声明开源许可证（核实日期 2026-09-29）。沿用其代码前，建议自行确认授权范围。

## WorkBuddy token 续期

> 频率：约 1~2 个月一次。

accessToken 有效期约 55 天（Keycloak 签发），过期后签到日志会提示
`accessToken 已过期`，PushPlus 推送内容中对应行显示 ❌。

token 存放在本机 WorkBuddy 数据目录中：新版客户端的正式会话文件
`workbuddy-desktop.info` 里 token 是**加密信封**（`$wbEncrypted`），读不出来；
但客户端轮换登录态时会留下**带时间戳的备份文件** `workbuddy-desktop.<时间戳>.info`，
其中保存着**明文 JWT**。`renew_token.py` 会自动扫描并提取最新的有效 token：

```bash
# 1) 只看提取结果（脱敏，不发网络请求）
python renew_token.py

# 2) 确认有效期没问题后，一键写入 GitHub Secrets（需已 gh auth login）
python renew_token.py --secret

# 3) 到仓库 Actions 页面手动 Run workflow 立即验证
```

如果脚本提示「没有有效期内的明文 token」：先在本机**打开一次 WorkBuddy 客户端**
（触发登录态刷新并生成新备份文件，等几秒），再重新运行。

### 已验证不可行的路径（别再试）

- ❌ **浏览器 Cookie**：`copilot.tencent.com` 的 API 只认 `Authorization: Bearer`，
  带 cookie 请求返回 `401 Authorization Required`（APISIX 网关，实测）。
- ❌ **workbuddy.cn 网页 Network 面板**：网页端请求只有统计/SDK（collect、v2_upload 等），
  抓不到 Authorization 头；登录态走 cookie，与签到 API 的 Bearer 认证不通用。

## ima 凭据抓取与续期

> 频率：约 30 天一次（refresh token 有效期 30 天，见下文「Token 生命周期」）。

### 原理（逆向结论，2026-09-25/26 实测验证）

ima 的「每日登录福利」页面是 **ima.qq.com 上的纯 Web SPA**（桌面客户端只是内嵌 webview 加载它），
领取动作走独立服务 `daily_login_activity`，纯 HTTP 即可完成：

| 端点 | 说明 | 请求体 |
|---|---|---|
| `POST https://ima.qq.com/cgi-bin/daily_login_activity/get_activity_info` | 查询签到状态 | `{}` |
| `POST https://ima.qq.com/cgi-bin/daily_login_activity/check_in` | 签到/领奖 | `{"type":1}` 每日签到；`{"type":2}` 连签 7 天满签奖励 |

认证头三件套：`x-ima-cookie`（cookie 串）、`x-ima-bkn`（由 `IMA-TOKEN` 做 DJB2 变体哈希实时计算，
脚本内置且已与抓包值比对一致）、`from_browser_ima: 1`（防伪头，缺失返回 `code 41`）。

关键业务码：`0` 成功；`1002` "today already checked in"（幂等信号，视为成功）；
`1100/1101/600001` 认证失效。活动槽位状态：`2`=已领取、`3`=今日可领、`4`=待解锁。

### Token 生命周期（2026-09-26 实测定论）

| Token | 有效期 | 说明 |
|---|---|---|
| access token（`IMA-TOKEN`） | **固定 2 小时**（`tokenValidTime=7200`） | 不适合直接长期保存 |
| refresh token | **30 天**（`refreshTokenValidTime=2592000`） | 用于换新 access token |

刷新端点：`POST /cgi-bin/auth_login/refresh`，请求体
`{"user_id","refresh_token","token_type"}`（bundle 中 `LoginHttpService.refresh`，
IsUnLoginTag 服务，**无需有效 access token**），响应 `{"code":0,"token":"...","token_valid_time":"7200"}`；
实测 refresh token 不轮换，可反复使用。

因此采用 **refresh 模式**：Secret `IMA_REFRESH` 存 JSON
（`user_id` / `refresh_token` / `token_type` / `static_fields` 设备指纹字段），
脚本每次运行先刷新换新 token 再签到——**约 30 天才需重新扫码一次**。
（脚本仍兼容旧的 `IMA_COOKIE` 模式作为回退，但 access token 只有 2 小时寿命，
该模式下几乎必然已过期，未在 workflow 中启用。）

### 抓取步骤

```bash
# 1) 本机运行抓取脚本（Playwright 打开 ima.qq.com，微信扫码登录）
#    需先安装 playwright-core，并把 NODE_PATH 指向其所在 node_modules
NODE_PATH="<node_modules 路径>" node tools/capture_ima.js

# 2) 从仓库根 .ima_capture/ 生成 IMA_REFRESH 凭据 JSON：
#    - refresh_token / user_id / token_type ← storage.json 的 localStorage
#      ima-universal-local-storage-accountInfo
#    - static_fields ← requests.json 里 x-ima-cookie 的
#      PLATFORM/CLIENT-TYPE/WEB-VERSION/IMA-GUID/IMA-Q36/IMA-IUA/UID-TYPE 七个字段
#    产物建议存为 ima_refresh.info（已被 .gitignore 忽略）

# 3) 一键更新 Secret
gh secret set IMA_REFRESH --repo <owner>/<repo> < ima_refresh.info

# 4) Actions → 每日签到总调度 → Run workflow 验证
```

### 已验证不可行的路径（ima，别再试）

- ❌ `activity_center/query_activity` 对活动类型 1010（每日登录送算力）返回
  `activityType not support`——该活动不走通用活动系统；
- ❌ ima 客户端 `--remote-debugging-port` 被屏蔽、Profile 历史库加密，客户端侧抓包不可行；
- ❌ 浏览器 cookie 里没有登录态（认证头由前端从 localStorage 的 accountInfo 构造）；
- ❌ 用 `IMA-REFRESH-TOKEN` 冒充 `IMA-TOKEN` 直接认证返回 `code 41`
  （refresh token 只能用于 `/refresh` 端点）。

## 雨云签到说明

### 为什么用 API Key 而不是账号密码

社区流传的雨云脚本基本都是**账密登录**（登录后拿 `x-csrf-token` 再签到），代价是要把
账号密码长期存在 Secrets 里，且每次运行都要过一次登录风控。本仓库改用雨云后台生成的
**API 密钥**（`x-api-key` 请求头）：

- 拿到密钥后**账号密码完全不落库**；
- 密钥可随时在后台吊销重发，泄露面比密码小；
- 没有登录环节，也就没有登录验证码问题。

获取方式：雨云后台 → **用户中心 → API 密钥** → 新建 → 复制，存为 Secret `RAINYUN_API_KEY`。

### 接口（2026-09-29 探测）

| 端点 | 方法 | 用途 |
|---|---|---|
| `/user/` | GET | 积分余额（`data.Points`） |
| `/user/reward/tasks` | GET | 积分任务列表（尝试解析「每日签到」`Status`） |
| `/user/reward/tasks` | POST | 领奖，body `{"task_name":"每日签到","verifyCode":""}` |

以**无效密钥**请求上述端点均返回 `{"code":30039,"message":"密钥认证错误或已失效"}` / HTTP 403，
据此可确认端点存在且认证方式为 `x-api-key`（不是 404、也不是 cookie 专属端点）。

### ⚠️ 已知约束：可能触发腾讯滑块验证码

雨云在风控命中时，领取「每日签到」会弹**腾讯滑块验证码**（前端 iframe id `tcaptcha_iframe_dy`）。
社区两种做法都是**回避**而非破解：

| 项目 | 处理方式 |
|---|---|
| `henjiu123/Rainyun-QingLong` | 浏览器自动化（Playwright/Selenium）+ `ddddocr`/打码服务，重且不稳定 |
| 345yun 下载量最高的流行版本 | 直接判定「需验证码，本次跳过」 |

本脚本采取同样克制的策略：**接口一旦提示需要验证码，就如实写入结果并结束**，
不接打码服务、不注入验证码、不做任何绕过。

因此**存在某几天签不上的可能**，推送里会显示：

```
⚠️ 雨云：需人工验证码，本次跳过（接口提示：xxx）
```

这是**预期行为，不是 Bug**。此时手动登录雨云点一下签到即可，次日脚本会继续尝试。

> 另注：GitHub Actions 的出口是数据中心 IP，触发风控/验证码的概率天然高于家用宽带。
> 若长期高频命中验证码，说明该方案在当前环境下收益有限，可考虑关闭该步骤。

### 积分价值参考（2026-09-29，来源为雨云官方文档与社区实测贴，**建议自行核实**）

- 每日签到约 **+500 积分**；新手一次性任务（绑定邮箱/手机/QQ/微信、加群）合计约 8500 分。
- 积分提现汇率 **2000 积分 = 1 元**，最低提现 60000 分（=30 元）→ 纯签到约 **0.25 元/天**。
- 积分商城：免费游戏云 2000 分兑 7 天，续期 2258 分/7 天 或 10000 分/30 天（库存每晚 20:00 刷新）。
- ⚠️ 雨云官方条款声明「**禁止使用非正常手段获取积分，若发现作弊现象将扣除所有积分，并保留封禁账号的权力**」。
  本脚本是单账号、低频、走官方接口的自动化，**仍可能被判定为「非正常手段」**——风险自担。

## 维护备忘

- **定时**：`sign_all.yml` cron `51 15 * * *`（UTC）= 北京 23:51，选冷门分钟以降低触发延迟
  （GitHub 定时触发仍可能有 0~30 分钟延迟）。
- **手动触发**：Actions → 每日签到总调度 → Run workflow。
  表单里勾选站点 = 只跑勾选项（不勾 = 跑全部）；勾 `skip_push` 则只跑不推送，便于调试。
  该开关只对手动触发生效，定时触发恒为「跑全部」，行为不受影响。
- **本地调试**：`WB_TOKEN=xxx WB_UID=xxx python workbuddy_checkin.py`；
  `IMA_REFRESH` 模式把凭据 JSON 存 `ima_refresh.info`（凭据文件均已 gitignore）。
- **退出码**：`workbuddy_checkin.py` / `ima_checkin.py` / `bilibili_checkin.py` / `rainyun_checkin.py` 一致 —— `0` 成功/已签，
  `2` 配置缺失，`4` 网络失败，`5` API 拒绝（含凭据失效；雨云的「需人工验证码」亦归此类）。
- **安全红线**：token / uid / cookie 只进 Secrets，不进代码和日志。
- **保活心跳**：公开仓库的定时工作流若连续 **60 天**无「仓库活动」（即无 commit push），
  会被 GitHub **自动停用**。本仓库常规运行只读代码、结果仅写本地并推送 PushPlus，
  **不产生任何 commit**，等于零活动 —— 转公开后第 60 天签到会被静默停掉。
  `sign_all.yml` 末尾的「保活心跳」步骤会在「距上次提交 > 45 天」时自动提交一个
  仅含时间戳的 `.heartbeat` 文件刷新计时器（约每 45 天 1 次，一年约 8 个 commit）。
  阈值取 45 而非 60，是为调度延迟与偶发漏跑留余量；**工作流一旦被停用，schedule 便
  不再触发，心跳也救不回来**，所以不能把阈值贴到 60。
  该步骤需 `permissions: contents: write`（本工作流仅由 schedule / workflow_dispatch
  触发，无 PR 注入面，故可开写权限）。

## 日志安全

本仓库公开，Actions 日志对所有访问者可见，因此「外部数据 → 日志」的输出一律先过 `logsafe.py`：

| 函数 | 用途 | 替代掉的旧写法 |
|---|---|---|
| `redact(text)` | 过滤 requestId/追踪码、UUID、凭据字段、裸 JWT、手机号、邮箱 | 直接打印响应体 / 异常文本 |
| `redact_obj(obj, limit)` | dict / list 序列化后脱敏并限长 | `print("查询响应:", info)` |
| `mask_secret(s)` | 凭据 → `sha256:xxxxxxxx (len=N)` 指纹 | `token[:6] + "..." + token[-6:]` |
| `mask_uid(uid)` | uid → 同上指纹（可跨运行比对是否换号） | `uid[:8] + "..."` |
| `mask_path(p)` | 路径中 `\Users\<名>`、`/home/<名>` → `***` | 打印完整本机路径 |

两条硬规则：

1. **先脱敏再截断**：写 `redact(body)[:400]`，不要写 `redact(body[:400])`。
   顺序反了会把凭据切成两半，正则就匹配不上。
2. **已登录页面的正文不打印**：页面 HTML 里的账号昵称 / uid 不在正则覆盖范围，
   只能整段省略 —— 脚本里改为只输出「页面长度 N 字符（正文含账号信息，已省略）」。

新加打印语句时请遵守同样的约定；新增脚本请 `from logsafe import ...` 复用，不要各自实现一份。
