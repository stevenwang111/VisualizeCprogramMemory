/**
 * 清空界面 + 长按强制结束 的界面验证（2560x1600 @150%）
 */
const { chromium } = require("playwright-core");

const URL = process.argv[2] || "http://127.0.0.1:8785/";

(async () => {
  const browser = await chromium.launch({ channel: "msedge", args: ["--no-sandbox", "--disable-gpu"] });
  const page = await browser.newPage({ viewport: { width: 1707, height: 1067 }, deviceScaleFactor: 1.5 });
  const errors = [];
  page.on("pageerror", (e) => errors.push("PAGEERROR: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push("CONSOLE: " + m.text()); });

  const cmds = [];
  page.on("response", async (r) => {
    if (!r.url().includes("/api/command")) return;
    try {
      const req = r.request().postData() || "";
      const body = await r.json();
      cmds.push(req + "  ->  " + JSON.stringify(body));
    } catch (e) { /* ignore */ }
  });

  const state = () => page.evaluate(async () => {
    const d = await (await fetch("/api/state")).json();
    return { snap: d.snapshot ? d.snapshot.state : null, phase: d.phase, bp: d.breakpoints };
  });

  const dom = () => page.evaluate(() => ({
    current: document.querySelectorAll(".src-line.current").length,
    cursor: document.querySelectorAll(".src-line.cursor").length,
    hexrows: document.querySelectorAll(".hexrow").length,
    vars: document.querySelectorAll(".var-card").length,
    stacks: document.querySelectorAll(".stack-item").length,
    ticks: document.querySelectorAll(".tl-tick").length,
    empty: document.querySelectorAll(".empty-state").length,
    badge: document.getElementById("status-badge").textContent,
    loc: document.getElementById("status-loc").textContent,
    clearDisabled: document.getElementById("btn-clear").disabled,
  }));

  await page.goto(URL, { waitUntil: "networkidle", timeout: 20000 });
  await page.waitForTimeout(800);
  console.log("加载后   ", JSON.stringify(await dom()), JSON.stringify(await state()));

  await page.click("#btn-debug");
  await page.waitForTimeout(1600);
  console.log("点调试后 ", JSON.stringify(await dom()), JSON.stringify(await state()));

  // 设个断点，验证清空后断点还在
  await page.click('.src-line[data-line="31"] .src-gutter');
  await page.waitForTimeout(600);

  // ---- 清空界面 ----
  await page.click("#btn-clear");
  await page.waitForTimeout(900);
  console.log("清空后   ", JSON.stringify(await dom()), JSON.stringify(await state()));
  await page.screenshot({ path: "shot_cleared.png" });

  // 刷新页面：清空必须是「真的」，不能一刷新又回来
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(900);
  console.log("刷新后   ", JSON.stringify(await dom()), JSON.stringify(await state()));

  // 清空之后还能接着调试
  await page.click("#btn-debug");
  await page.waitForTimeout(1600);
  console.log("再调试   ", JSON.stringify(await dom()), JSON.stringify(await state()));

  // ---- 长按停止 = 强制结束 ----
  const btn = await page.$("#btn-stop");
  const box = await btn.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(500);
  const mid = await page.evaluate(() => {
    const f = document.querySelector("#btn-stop .hold-fill");
    return {
      width: f ? f.style.width : "none",
      holding: document.getElementById("btn-stop").classList.contains("holding"),
    };
  });
  await page.screenshot({ path: "shot_hold_mid.png" });
  console.log("按住 0.5s", JSON.stringify(mid));
  await page.waitForTimeout(800);           // 累计 1.3s，越过阈值（1s）
  await page.mouse.up();
  await page.waitForTimeout(900);
  console.log("长按释放后", JSON.stringify(await dom()));
  const after = await page.evaluate(() => ({
    fill: document.querySelector("#btn-stop .hold-fill").style.width,
    holding: document.getElementById("btn-stop").classList.contains("holding"),
  }));
  console.log("进度条复位", JSON.stringify(after));

  console.log("\n--- 命令应答 ---");
  cmds.forEach((c) => console.log("  " + c));
  console.log(errors.length ? "\n页面错误:\n" + errors.join("\n") : "\n无页面错误");
  await browser.close();
})().catch((e) => { console.error("FAILED:", e.message); process.exit(1); });
