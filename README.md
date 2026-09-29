# my-daily-checkin

基于 GitHub Actions 的多站点每日自动签到合集：4 个论坛 + WorkBuddy 积分签到 + 腾讯 ima 每日登录领算力 + 哔哩哔哩每日任务 + 雨云每日签到。定时任务在云端运行，签到结果通过 [PushPlus](https://www.pushplus.plus/) 推送到微信，仓库本身不保存任何运行记录。

> 本项目仅供学习交流，请遵守各站点用户协议，请勿用于商业用途。

## 脚本说明

| 脚本 | 目标站点 | 实现方式 | 所需 Secrets |
|---|---|---|---|
| `enshan.py` | 恩山论坛 (right.com.cn) | 纯 HTTP 签到（参考 [Sitoi/dailycheckin](https://github.com/Sitoi/dailycheckin) 思路），自动提取 formhash，抓取今日/累计积分与连签天数 | `ESHAN_COOKIE`、`USER_UID` |
| `fnclub.py` | 飞牛论坛 (club.fnnas.com) | 纯 HTTP 签到，内置阿里云 WAF `acw_sc__v2` Cookie 挑战自动解算（Node 执行站点算法） | `FNOS_COOKIE` |
| `znds.py` | 智能电视网 (znds.com) | 账号密码登录后签到，抓取金币/威望/Z币/积分 | `ZNDS_USERNAME`、`ZNDS_PASSWORD` |
| `hifiti.js` | HIFITI 论坛 (hifiti.com) | Node 纯 HTTP 签到，支持 JSON 数组多账号并发，带网络层重试（移植自 [ewigl/hifini-auto-checkin](https://github.com/ewigl/hifini-auto-checkin)） | `HIFITI_ACCOUNTS` |
| `workbuddy_checkin.py` | WorkBuddy (copilot.tencent.com) | Bearer 认证调 `daily-checkin`，幂等（`code=10001` 视为已签），成功后抓取积分概览（签到逻辑参考 [wangmingdong/workbuddy-signin](https://github.com/wangmingdong/workbuddy-signin)） | `WB_TOKEN`、`WB_UID` |
| `ima_checkin.py` | 腾讯 ima (ima.qq.com) | refresh 模式换新 access token 后调 `daily_login_activity`，先查后签，含满签奖励延迟解锁重试 | `IMA_REFRESH` |
| `bilibili_checkin.py` | 哔哩哔哩 (bilibili.com) | Cookie 认证，默认执行投币 / 分享 / 观看视频每日任务（投币带来源回退与已投去重）；漫画签到 / 银瓜子兑换 / 应援团签到可用 `TASK_CONFIG` 按需开启。写 `checkin_results.txt` 交汇总，不自行推送 | `BILIBILI_COOKIE` |
| `rainyun_checkin.py` | 雨云 (rainyun.com) | API Key（`x-api-key`）纯 HTTP 领「每日签到」，签到前后各取一次积分算增量；**风控命中时判「需人工验证码」直接跳过，不接打码、不绕验证**（详见 [雨云签到说明](MAINTENANCE.md#雨云签到说明)） | `RAINYUN_API_KEY` |
| `daily_push.py` | PushPlus | 汇总本轮所有签到结果，HTML 模板推送到微信，推送后清空结果文件 | `PUSHPLUS_TOKEN` |

## 运行流程

- **`sign_all.yml`（每日签到总调度）**：每天北京时间 **23:51**（cron `51 15 * * *` UTC，GitHub 定时可能有 0~30 分钟延迟）顺序执行恩山 → 飞牛 → 智能电视网 → HIFITI → WorkBuddy → ima → 哔哩哔哩 → 雨云 八个签到，最后统一推送 PushPlus。
- **手动补跑指定站点**：Actions → 每日签到总调度 → **Run workflow**，在表单里勾选要跑的站点，则**只跑勾选项**（常用于某个站点当日失败后单独补跑，不必等次日、也不会重复跑其他站点）；**一个都不勾 = 跑全部**，与定时触发完全一致。另有 `skip_push` 选项可在调试时不发推送。
- **失败隔离**：所有签到步骤均为 `continue-on-error: true`，单个站点失败不会中断后续任务；成败信息统一体现在 PushPlus 推送内容中（不依赖 Actions 失败邮件）。
- 签到结果只写入本地临时文件 `checkin_results.txt`（已被 `.gitignore` 忽略），推送 PushPlus 后清空，**不写入 README、不提交到仓库**。
- 所有凭据通过 GitHub Secrets 注入环境变量，代码中不含任何敏感信息；脚本输出已脱敏（不打印用户名/Cookie/Token）。

## 部署步骤

1. Fork 或克隆本仓库。
2. 在仓库 **Settings → Secrets and variables → Actions** 中按上表配置所需 Secrets：
   - Cookie 类：浏览器登录站点后，从开发者工具 Network 面板复制请求头中的 `Cookie` 字段；
   - `HIFITI_ACCOUNTS` 为 JSON 数组：`[{"cookie": "xxx"}, {"cookie": "yyy"}]`；
   - `WB_TOKEN` / `WB_UID`：见 [WorkBuddy token 续期](MAINTENANCE.md#workbuddy-token-续期)；
   - `IMA_REFRESH`：见 [ima 凭据抓取与续期](MAINTENANCE.md#ima-凭据抓取与续期)；
   - `BILIBILI_COOKIE`：B 站 Cookie 全文，须含 `SESSDATA` 与 `bili_jct`（后者作为写操作的 csrf 参数，缺失会导致投币/分享静默失败）。
   - `RAINYUN_API_KEY`：雨云 API 密钥（**不是**账号密码），见 [雨云签到说明](MAINTENANCE.md#雨云签到说明)。
   - 哔哩哔哩可选变量（均有默认值，不配也能跑）：`TASK_CONFIG`（默认 `add_coin,share_video,watch_video`；可追加 `live_sign` / `manga_sign` / `silver2coin` / `link_sign`，逗号分隔）、`COIN_ADD_NUM`（每日投币数，默认 `1`）、`COIN_SELECT_LIKE`（投币时是否同时点赞，`1`/`0`，默认 `1`）、`COIN_VIDEO_SOURCE`（投币视频来源 `ranking` / `dynamic`，默认 `ranking`）。
3. 在 **Actions** 页面对 `每日签到总调度` 手动 **Run workflow** 验证，微信收到 PushPlus 推送即部署成功。

## 更多说明

上游项目对照与二次开发明细、目录结构、凭据续期（WorkBuddy / ima）、雨云签到说明、维护备忘、日志安全，见 **[维护与二次开发说明](MAINTENANCE.md)**。

## License

[MIT](LICENSE)

其中 `bilibili_checkin.py` 移植自 [dangks/bilibili_checkin](https://github.com/dangks/bilibili_checkin)，
沿用其 MIT 许可。
