# my-daily-checkin

基于 GitHub Actions 的多站点每日自动签到合集：4 个论坛 + WorkBuddy 积分签到 + 腾讯 ima 每日登录领算力。定时任务在云端运行，签到结果通过 [PushPlus](https://www.pushplus.plus/) 推送到微信，仓库本身不保存任何运行记录。

> 本项目仅供学习交流，请遵守各站点用户协议，请勿用于商业用途。

## 脚本说明

| 脚本 | 目标站点 | 实现方式 | 所需 Secrets |
|---|---|---|---|
| `enshan.py` | 恩山论坛 (right.com.cn) | 纯 HTTP 签到（参考 [Sitoi/dailycheckin](https://github.com/Sitoi/dailycheckin) 思路），自动提取 formhash，抓取今日/累计积分与连签天数 | `ESHAN_COOKIE`、`USER_UID` |
| `fnclub.py` | 飞牛论坛 (club.fnnas.com) | 纯 HTTP 签到，内置阿里云 WAF `acw_sc__v2` Cookie 挑战自动解算（Node 执行站点算法） | `FNOS_COOKIE` |
| `znds.py` | 智能电视网 (znds.com) | 账号密码登录后签到，抓取金币/威望/Z币/积分 | `ZNDS_USERNAME`、`ZNDS_PASSWORD` |
| `hifiti.js` | HIFITI 论坛 (hifiti.com) | Node 纯 HTTP 签到，支持 JSON 数组多账号并发，带网络层重试 | `HIFITI_ACCOUNTS` |
| `workbuddy_checkin.py` | WorkBuddy (copilot.tencent.com) | Bearer 认证调 `daily-checkin`，幂等（`code=10001` 视为已签），成功后抓取积分概览 | `WB_TOKEN`、`WB_UID` |
| `ima_checkin.py` | 腾讯 ima (ima.qq.com) | refresh 模式换新 access token 后调 `daily_login_activity`，先查后签，含满签奖励延迟解锁重试 | `IMA_REFRESH` |
| `daily_push.py` | PushPlus | 汇总本轮所有签到结果，HTML 模板推送到微信，推送后清空结果文件 | `PUSHPLUS_TOKEN` |
| `notify.py` | PushPlus | 独立推送工具函数，可被其他脚本 import 复用或单独测试 | `PUSHPLUS_TOKEN` |

## 运行流程

- **`sign_all.yml`（每日签到总调度）**：每天北京时间 **00:09**（cron `9 16 * * *` UTC，GitHub 定时可能有 0~30 分钟延迟）顺序执行恩山 → 飞牛 → 智能电视网 → HIFITI → WorkBuddy → ima 六个签到，最后统一推送 PushPlus。也支持在 Actions 页面手动触发。
- **失败隔离**：所有签到步骤均为 `continue-on-error: true`，单个站点失败不会中断后续任务；成败信息统一体现在 PushPlus 推送内容中（不依赖 Actions 失败邮件）。
- 签到结果只写入本地临时文件 `checkin_results.txt`（已被 `.gitignore` 忽略），推送 PushPlus 后清空，**不写入 README、不提交到仓库**。
- 所有凭据通过 GitHub Secrets 注入环境变量，代码中不含任何敏感信息；脚本输出已脱敏（不打印用户名/Cookie/Token）。

## 部署步骤

1. Fork 或克隆本仓库。
2. 在仓库 **Settings → Secrets and variables → Actions** 中按上表配置所需 Secrets：
   - Cookie 类：浏览器登录站点后，从开发者工具 Network 面板复制请求头中的 `Cookie` 字段；
   - `HIFITI_ACCOUNTS` 为 JSON 数组：`[{"cookie": "xxx"}, {"cookie": "yyy"}]`；
   - `WB_TOKEN` / `WB_UID`：见下文「WorkBuddy token 续期」；
   - `IMA_REFRESH`：见下文「ima 凭据抓取与续期」。
3. 在 **Actions** 页面对 `每日签到总调度` 手动 **Run workflow** 验证，微信收到 PushPlus 推送即部署成功。

## WorkBuddy token 续期（约 1~2 个月一次）

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

## ima 凭据抓取与续期（约 30 天一次）

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

## 目录结构

```
.
├── .github/workflows/
│   └── sign_all.yml        # 每日总调度（北京时间 00:09，六站点串行 + PushPlus 汇总）
├── enshan.py               # 恩山论坛签到
├── fnclub.py               # 飞牛论坛签到（含 WAF 解算）
├── znds.py                 # 智能电视网签到
├── hifiti.js               # HIFITI 论坛签到（Node，多账号）
├── workbuddy_checkin.py    # WorkBuddy 积分签到
├── ima_checkin.py          # 腾讯 ima 每日登录领算力
├── renew_token.py          # WorkBuddy token 一键续期（本机运行）
├── daily_push.py           # PushPlus 汇总推送
├── notify.py               # PushPlus 推送工具函数
└── tools/
    └── capture_ima.js      # ima 登录态抓取（本机一次性运行，需 playwright-core）
```

## 维护备忘

- **定时**：`sign_all.yml` cron `9 16 * * *`（UTC）= 北京 00:09，选冷门分钟以降低触发延迟
  （GitHub 定时触发仍可能有 0~30 分钟延迟）。
- **手动触发**：Actions → 每日签到总调度 → Run workflow。
- **本地调试**：`WB_TOKEN=xxx WB_UID=xxx python workbuddy_checkin.py`；
  `IMA_REFRESH` 模式把凭据 JSON 存 `ima_refresh.info`（凭据文件均已 gitignore）。
- **退出码**：`workbuddy_checkin.py` / `ima_checkin.py` 一致 —— `0` 成功/已签，
  `2` 配置缺失，`4` 网络失败，`5` API 拒绝（含凭据失效）。
- **安全红线**：token / uid / cookie 只进 Secrets，不进代码和日志。

## License

[MIT](LICENSE)
