/* ima 登录态抓取脚本（一次性）
 * 流程：打开 ima.qq.com → 等用户扫码登录 → 被动捕获全部 cgi-bin 请求（含认证头）
 *      → 自动探索"每日登录送算力"入口并尝试点击领取 → 保存 cookies/localStorage/请求记录
 * 产物：仓库根 .ima_capture/ 下的 storage.json / requests.json / ui_snapshot.txt / qr.png
 * 用法：NODE_PATH=<playwright-core 所在 node_modules> node tools/capture_ima.js
 */
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const OUT = path.join(__dirname, '..', '.ima_capture');
fs.mkdirSync(OUT, { recursive: true });

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: false });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  const reqs = [];
  ctx.on('request', (req) => {
    const u = req.url();
    if (/cgi-bin|incentive|activity|claim|coupon|credit|token/i.test(u) && !/\.(js|css|png|jpg|svg|woff)/.test(u)) {
      reqs.push({ kind: 'req', url: u, method: req.method(),
                  headers: req.headers(), postData: (req.postData() || '').slice(0, 3000) });
    }
  });
  ctx.on('response', async (res) => {
    const u = res.url();
    if (/incentive|activity|claim|coupon/i.test(u) && !/\.(js|css|png|jpg|svg|woff)/.test(u)) {
      try {
        reqs.push({ kind: 'resp', url: u, status: res.status(),
                    body: (await res.text()).slice(0, 4000) });
      } catch {}
    }
  });

  await page.goto('https://ima.qq.com/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3000);
  try { await page.getByText('登录', { exact: true }).first().click({ timeout: 5000 }); } catch (e) {}
  await page.waitForTimeout(2500);
  try { await page.screenshot({ path: path.join(OUT, 'qr.png') }); console.log('[截图] 登录弹窗已截图 -> .ima_capture/qr.png'); } catch (e) {}

  console.log('[等待] 请在浏览器窗口中扫码登录 ima（最长等 8 分钟）...');
  let loggedIn = false;
  for (let i = 0; i < 240; i++) {
    await page.waitForTimeout(2000);
    try {
      const loginBtn = await page.getByText('登录', { exact: true }).count();
      if (loginBtn === 0) { loggedIn = true; break; }
      // 每 30 秒确认登录弹窗还在,若弹窗消失则重新点击
      if (i % 15 === 14) {
        console.log(`[等待] ${((i + 1) * 2)}s 仍未登录,重试点击登录按钮...`);
        await page.screenshot({ path: path.join(OUT, 'waiting.png') });
        try { await page.getByText('登录', { exact: true }).first().click({ timeout: 3000 }); } catch (e) {}
      }
    } catch (e) { loggedIn = true; break; }
  }
  console.log('[状态] 登录:', loggedIn);
  if (!loggedIn) { await browser.close(); process.exit(2); }

  await page.waitForTimeout(4000);
  const cookies = await ctx.cookies();
  const ls = await page.evaluate(() => Object.fromEntries(Object.entries(localStorage)));
  const ss = await page.evaluate(() => Object.fromEntries(Object.entries(sessionStorage)));
  fs.writeFileSync(path.join(OUT, 'storage.json'),
    JSON.stringify({ cookies, localStorage: ls, sessionStorage: ss }, null, 2));
  console.log('[存储] cookies=%d localStorage=%d sessionStorage=%d',
    cookies.length, Object.keys(ls).length, Object.keys(ss).length);

  // ---- 自动探索:找"每日登录送算力"入口 ----
  const tryClickTexts = ['用量统计', '我的copilot', '个人中心', '每日登录', '签到', '算力', '领取', '立即领取'];
  for (const t of tryClickTexts) {
    try {
      const el = page.locator(`text=${t}`).first();
      if (await el.count() > 0 && await el.isVisible()) {
        console.log('[探索] 点击:', t);
        await el.click({ timeout: 3000 });
        await page.waitForTimeout(2500);
      }
    } catch (e) { console.log('[探索] 点击失败:', t, e.message.split('\n')[0]); }
  }

  // 页面文案快照（判断签到页内容）
  try {
    const text = await page.evaluate(() => document.body.innerText.slice(0, 6000));
    fs.writeFileSync(path.join(OUT, 'ui_snapshot.txt'), text);
  } catch (e) {}

  // 再等 15s 收尾（让迟到的活动请求进来）
  await page.waitForTimeout(15000);
  fs.writeFileSync(path.join(OUT, 'requests.json'), JSON.stringify(reqs, null, 2));
  console.log('[抓包] 共捕获', reqs.length, '条 -> requests.json');

  // 保持浏览器 3 分钟供人工补做（如需手点页面）
  console.log('[保持] 浏览器保持 3 分钟，如需手动在页面点"领取"请现在操作...');
  await page.waitForTimeout(180000);
  fs.writeFileSync(path.join(OUT, 'requests.json'), JSON.stringify(reqs, null, 2));
  console.log('[完成] 最终捕获', reqs.length, '条');
  await browser.close();
})().catch(e => { console.error('[异常]', e); process.exit(1); });
