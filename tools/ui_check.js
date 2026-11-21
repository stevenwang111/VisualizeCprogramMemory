/**
 * 断点开关 + 悬停配色的界面验证
 * 视口按 2560x1600 @150% 换算：CSS 视口 1707x1067，DPR 1.5
 */
const { chromium } = require("playwright-core");

const URL = process.argv[2] || "http://127.0.0.1:8781/";

(async () => {
  const browser = await chromium.launch({
    channel: "msedge",
    args: ["--no-sandbox", "--disable-gpu"],
  });
  const page = await browser.newPage({
    viewport: { width: 1707, height: 1067 },
    deviceScaleFactor: 1.5,
  });

  const errors = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push("CONSOLE: " + m.text()); });
  page.on("pageerror", (e) => errors.push("PAGEERROR: " + e.message));

  await page.goto(URL, { waitUntil: "networkidle", timeout: 20000 });
  await page.waitForTimeout(1200);

  const state = async () => {
    const r = await page.evaluate(async () => {
      const resp = await fetch("/api/state");
      const d = await resp.json();
      return d.breakpoints || [];
    });
    return r;
  };
  const dots = async () => page.evaluate(() =>
    Array.from(document.querySelectorAll(".src-gutter.has-bp"))
      .map((g) => g.parentElement.dataset.line).join(","));

  const gutter = (n) => `.src-line[data-line="${n}"] .src-gutter`;

  const report = [];
  const step = async (label) => {
    const d = await dots(); const s = await state();
    report.push(`${label.padEnd(34)} 界面红点=[${d}]  后端=[${s}]`);
    return { d, s };
  };

  await step("初始");

  // 1) 设 26
  await page.click(gutter(26));
  await page.waitForTimeout(600);
  await step("点 26（设断点）");

  // 2) 再点一次同一个位置（取消）
  await page.click(gutter(26));
  await page.waitForTimeout(700);
  await step("再点 26（取消断点）");

  // 3) 设 26 + 31，然后取消 26，看 31 会不会把 26 带回来
  await page.click(gutter(26));
  await page.waitForTimeout(500);
  await page.click(gutter(31));
  await page.waitForTimeout(500);
  await step("设 26 + 31");
  await page.click(gutter(26));
  await page.waitForTimeout(500);
  await step("取消 26");
  await page.click(gutter(32));
  await page.waitForTimeout(900);
  await step("再设 32（看 26 是否复活）");

  // 4) 吸附行：点第 30 行（空行，GDB 落到 31 —— 31 已有断点）
  await page.click(gutter(30));
  await page.waitForTimeout(700);
  await step("点 30（空行，应吸附到 31 且不重复）");

  // 清理：把剩下的都取消掉
  for (const n of [31, 32]) {
    await page.click(gutter(n)).catch(() => {});
    await page.waitForTimeout(400);
  }
  await step("清理后");

  // 5) 悬停配色：普通行 / 光标行 / 当前执行行
  await page.click('.src-line[data-line="24"] .src-text');
  await page.waitForTimeout(300);
  await page.click("#btn-debug").catch(() => {});
  await page.waitForTimeout(1500);

  const rowBg = async (line) => page.evaluate((n) => {
    const li = document.querySelector(`.src-line[data-line="${n}"]`);
    if (!li) return "no-line";
    return {
      bg: getComputedStyle(li).backgroundColor,
      gutter: getComputedStyle(li.querySelector(".src-gutter")).color,
      cls: li.className,
    };
  }, line);

  const plainIdle = await rowBg(23);       // 普通行（未悬停）
  await page.hover('.src-line[data-line="23"] .src-text');
  await page.waitForTimeout(200);
  const plainHover = await rowBg(23);
  await page.screenshot({ path: "shot_break_flow.png" });

  const cursorRow = await rowBg(24);        // 光标行（绿）
  const currentRow = await rowBg(25);       // 当前执行行（蓝）—— 看它在哪

  // 找真正的 current 行
  const cur = await page.evaluate(() => {
    const el = document.querySelector(".src-line.current");
    if (!el) return null;
    return { line: el.dataset.line, bg: getComputedStyle(el).backgroundColor };
  });

  console.log(report.join("\n"));
  console.log("\n--- 配色 ---");
  console.log("普通行(静)  :", JSON.stringify(plainIdle));
  console.log("普通行(悬停):", JSON.stringify(plainHover));
  console.log("光标行      :", JSON.stringify(cursorRow));
  console.log("当前执行行  :", JSON.stringify(cur));
  console.log("第25行      :", JSON.stringify(currentRow));

  await page.screenshot({ path: "shot_break_hover.png" });
  if (errors.length) { console.log("\n--- 页面错误 ---"); errors.forEach((e) => console.log("  " + e)); }
  else console.log("\n无页面错误");
  await browser.close();
})().catch((e) => { console.error("FAILED:", e.message); process.exit(1); });
