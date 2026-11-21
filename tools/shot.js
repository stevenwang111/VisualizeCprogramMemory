/**
 * cdbg 界面截图脚本（用 playwright-core 驱动系统 Edge）
 *
 * 用法：node shot.js <url> <输出png> [点击的按钮id...]
 */
const { chromium } = require("playwright-core");

(async () => {
  const url = process.argv[2] || "http://127.0.0.1:8779/";
  const out = process.argv[3] || "shot.png";
  const clicks = process.argv.slice(4);

  const browser = await chromium.launch({
    channel: "msedge",
    args: ["--no-sandbox", "--disable-gpu"],
  });
  const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });

  const errors = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push("CONSOLE: " + m.text());
  });
  page.on("pageerror", (e) => errors.push("PAGEERROR: " + e.message));

  await page.goto(url, { waitUntil: "networkidle", timeout: 20000 });
  await page.waitForTimeout(1500);

  for (const spec of clicks) {
    const [id, countRaw] = spec.split(":");
    const count = countRaw ? Number(countRaw) : 1;
    for (let i = 0; i < count; i++) {
      try {
        await page.click("#" + id, { timeout: 5000 });
        await page.waitForTimeout(700);
      } catch (e) {
        errors.push("CLICK FAILED " + id + ": " + e.message.split("\n")[0]);
        break;
      }
    }
  }

  // 抓一些页面状态，方便确认渲染是否成功
  const info = await page.evaluate(() => ({
    regions: document.querySelectorAll(".region").length,
    hexRows: document.querySelectorAll(".hexrow").length,
    varCards: document.querySelectorAll(".var-card").length,
    stackItems: document.querySelectorAll(".stack-item").length,
    lessons: document.querySelectorAll(".lesson").length,
    srcLines: document.querySelectorAll(".src-line").length,
    currentLine: document.querySelectorAll(".src-line.current").length,
    ticks: document.querySelectorAll(".tl-tick").length,
    ptrPaths: document.querySelectorAll("#ptr-layer path").length,
    legend: document.querySelectorAll(".legend-item").length,
  }));

  await page.screenshot({ path: out, fullPage: false });
  console.log(JSON.stringify(info, null, 1));
  if (errors.length) {
    console.log("--- 页面错误 ---");
    errors.forEach((e) => console.log("  " + e));
  } else {
    console.log("无页面错误");
  }
  await browser.close();
})().catch((e) => {
  console.error("FAILED:", e.message);
  process.exit(1);
});
