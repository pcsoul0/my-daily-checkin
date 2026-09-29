import { appendFileSync } from "fs";

const signPageUrl = "https://www.hifiti.com/sg_sign.htm";
const responseSuccessCode = "0";
const siteName = "HIFITI论坛签到";

// 北京时间（UTC+8，无夏令时）当前时间，格式固定为 YYYY-MM-DD HH:MM:SS
// 与 enshan.py / fnclub.py / znds.py 的 strftime("%Y-%m-%d %H:%M:%S") 保持一致
function formatNow() {
  const d = new Date(Date.now() + 8 * 3600 * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return (
    `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
    `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`
  );
}

// 带重试的 fetch：仅对网络层瞬时错误与 HTTP 5xx 重试；4xx / 业务失败不重试
async function fetchWithRetry(url, options = {}, { retries = 3, baseDelay = 3000 } = {}) {
  let lastErr;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      // 每次请求独立设置 20s 超时，避免连接挂起卡死
      const opts = { ...options, signal: options.signal || AbortSignal.timeout(20000) };
      const response = await fetch(url, opts);
      // 5xx 视为可重试的服务端临时错误
      if (response.status >= 500 && response.status <= 599) {
        throw new Error(`HTTP ${response.status}`);
      }
      return response;
    } catch (err) {
      lastErr = err;
      const msg = String((err && err.message) || err);
      const name = (err && err.name) || "";
      const retryable =
        err instanceof TypeError || // Node fetch 网络层失败（fetch failed）
        name === "TimeoutError" || // AbortSignal.timeout 触发
        /fetch failed|ECONN|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|socket hang up|HTTP 5\d\d/i.test(msg);
      if (!retryable || attempt === retries) {
        throw err; // 不可重试或次数用尽，抛出最终错误
      }
      const delay = baseDelay * Math.pow(2, attempt);
      console.warn(`⚠️ 第 ${attempt + 1} 次请求失败（${msg}），${delay / 1000}s 后重试...`);
      await new Promise((r) => setTimeout(r, delay));
    }
  }
  throw lastErr;
}

async function checkIn(account) {
  // 不输出登录用户名，仅输出站点名
  console.log(`${siteName}：开始签到...`);

  const response = await fetchWithRetry(signPageUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
      "X-Requested-With": "XMLHttpRequest",
      "User-Agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
      Cookie: account.cookie,
    },
  });

  if (!response.ok) {
    throw new Error(`网络请求出错 - ${response.status}`);
  }

  const responseJson = await response.json();

  // ✅ 签到成功
  if (responseJson.code === responseSuccessCode) {
    return responseJson.message || "签到成功";
  }

  // ✅ 今天已签过，正常返回，不报错（接口实际仅返回"今天已经签过啦！"，改用包含匹配兼容前缀差异）
  if (responseJson.message && responseJson.message.includes("已经签过啦")) {
    return responseJson.message;
  }

  // ❌ 其他才是真失败（时间戳与站点名由 writeResult 统一拼装，此处不再重复）
  const detail = responseJson.message || JSON.stringify(responseJson);
  throw new Error(`失败: ${detail}`);
}

async function processSingleAccount(account) {
  return await checkIn(account);
}

// 写入 checkin_results.txt（不输出登录用户名）
// 统一推送行格式：`时间 - <✅/❌> HIFITI论坛签到 详情`
//   - 状态符号提到站点名之前（接口返回的 message 自带 ✅，原先落在站点名之后）
//   - 原 record 前置了一个 "\n"，会与上一块尾部的空行叠成 3 个换行，
//     在 PushPlus 里渲染出 2 个空行，故去掉
function writeResult(content) {
  try {
    const now = formatNow();
    const lines = String(content)
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean);
    const record =
      lines
        .map((line) => {
          const m = /^([✅❌ℹ️⚠️])\s*(.*)$/.exec(line);
          const status = m ? m[1] : "ℹ️";
          const detail = m ? m[2] : line;
          return `${now} - ${status} ${siteName} ${detail}`;
        })
        .join("\n") + "\n\n";
    appendFileSync("checkin_results.txt", record, "utf8");
    console.log("\n✅ 签到结果已成功追加到 checkin_results.txt");
  } catch (err) {
    console.error("\n❌ 写入 checkin_results.txt 失败:", err.message);
  }
}

async function main() {
  let accounts;

  try {
    if (!process.env.ACCOUNTS) throw new Error("未配置账户信息");
    accounts = JSON.parse(process.env.ACCOUNTS);
  } catch (error) {
    const msg = `❌ ${error.message.includes("JSON") ? "账户格式错误" : error.message}`;
    console.error(msg);
    writeResult(msg);
    process.exit(1);
  }

  const results = await Promise.allSettled(
    accounts.map(processSingleAccount)
  );

  console.log("\n======== 签到结果 ========\n");

  let hasRealError = false;
  const lines = [];

  results.forEach((result) => {
    if (result.status === "fulfilled") {
      // ✅ 成功 / 已签到 都算正常
      lines.push(`✅ ${result.value}`);
    } else {
      // ❌ 只有真失败才标记错误
      hasRealError = true;
      lines.push(`❌ ${result.reason.message}`);
    }
  });

  const output = lines.join("\n");
  console.log(output);
  writeResult(output);

  // 只有真失败才退出码1
  process.exit(hasRealError ? 1 : 0);
}

main();
