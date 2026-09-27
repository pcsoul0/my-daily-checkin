# my-daily-checkin

基于 GitHub Actions 的多论坛每日自动签到合集。定时任务在云端运行，签到结果通过 [PushPlus](https://www.pushplus.plus/) 推送到微信，仓库本身不保存任何运行记录。

> 本项目仅供学习交流，请遵守各站点用户协议，请勿用于商业用途。

## 脚本说明

| 脚本 | 目标站点 | 实现方式 | 所需 Secrets |
|---|---|---|---|
| `enshan.py` | 恩山论坛 (right.com.cn) | 纯 HTTP 签到（参考 [Sitoi/dailycheckin](https://github.com/Sitoi/dailycheckin) 思路），自动提取 formhash，抓取今日/累计积分与连签天数 | `ESHAN_COOKIE`、`USER_UID` |
| `fnclub.py` | 飞牛论坛 (club.fnnas.com) | 纯 HTTP 签到，内置阿里云 WAF `acw_sc__v2` Cookie 挑战自动解算（Node 执行站点算法） | `FNOS_COOKIE` |
| `znds.py` | 智能电视网 (znds.com) | 账号密码登录后签到，抓取金币/威望/Z币/积分 | `ZNDS_USERNAME`、`ZNDS_PASSWORD` |
| `hifiti.js` | HIFITI 论坛 (hifiti.com) | Node 纯 HTTP 签到，支持 JSON 数组多账号并发，带网络层重试 | `HIFITI_ACCOUNTS` |
| `daily_push.py` | PushPlus | 汇总本轮所有签到结果，HTML 模板推送到微信，推送后清空结果文件 | `PUSHPLUS_TOKEN` |
| `notify.py` | PushPlus | 独立推送工具函数，可被其他脚本 import 复用或单独测试 | `PUSHPLUS_TOKEN` |

## 运行流程

- **`sign_all.yml`（每日签到总调度）**：每天北京时间 **00:09**（cron `9 16 * * *` UTC，GitHub 定时可能有 0~30 分钟延迟）顺序执行恩山 → 飞牛 → 智能电视网 → HIFITI 四个签到，最后统一推送 PushPlus。也支持在 Actions 页面手动触发。
- 签到结果只写入本地临时文件 `checkin_results.txt`（已被 `.gitignore` 忽略），推送 PushPlus 后清空，**不写入 README、不提交到仓库**。
- 所有凭据通过 GitHub Secrets 注入环境变量，代码中不含任何敏感信息；脚本输出已脱敏（不打印用户名/Cookie）。

## 部署步骤

1. Fork 或克隆本仓库。
2. 在仓库 **Settings → Secrets and variables → Actions** 中按上表配置所需 Secrets：
   - Cookie 类：浏览器登录站点后，从开发者工具 Network 面板复制请求头中的 `Cookie` 字段；
   - `HIFITI_ACCOUNTS` 为 JSON 数组：`[{"cookie": "xxx"}, {"cookie": "yyy"}]`。
3. 在 **Actions** 页面对 `每日签到总调度` 手动 **Run workflow** 验证，微信收到 PushPlus 推送即部署成功。

## 目录结构

```
.
├── .github/workflows/
│   └── sign_all.yml        # 每日总调度（北京时间 00:09）
├── enshan.py               # 恩山论坛签到
├── fnclub.py               # 飞牛论坛签到（含 WAF 解算）
├── znds.py                 # 智能电视网签到
├── hifiti.js               # HIFITI 论坛签到（Node，多账号）
├── daily_push.py           # PushPlus 汇总推送
└── notify.py               # PushPlus 推送工具函数
```

## License

[MIT](LICENSE)
