/** 程序卡在 scanf（phase=running）时长按停止 → taskkill /F */
const { chromium } = require("playwright-core");
const URL = process.argv[2] || "http://127.0.0.1:8786/";

(async () => {
  const browser = await chromium.launch({ channel: "msedge", args: ["--no-sandbox", "--disable-gpu"] });
  const page = await browser.newPage({ viewport: { width: 1707, height: 1067 }, deviceScaleFactor: 1.5 });
  const errors = [];
  page.on("pageerror", (e) => errors.push("PAGEERROR: " + e.message));
  const cmds = [];
  page.on("response", async (r) => {
    if (!r.url().includes("/api/command")) return;
    try { cmds.push((r.request().postData() || "") + " -> " + JSON.stringify(await r.json())); } catch (e) {}
  });

  await page.goto(URL, { waitUntil: "networkidle", timeout: 20000 });
  await page.waitForTimeout(800);
  await page.click("#btn-debug");
  await page.waitForTimeout(1200);
  // 继续运行 → 撞上 scanf 卡住，phase 变 running，这才是长按真正要救的场景
  await page.click("#btn-continue");
  await page.waitForTimeout(1500);
  const runState = await page.evaluate(async () => {
    const d = await (await fetch("/api/state")).json();
    return { phase: d.phase, snap: d.snapshot && d.snapshot.state, pid: d.snapshot && d.snapshot.pid,
             badge: document.getElementById("status-badge").textContent,
             stopDisabled: document.getElementById("btn-stop").disabled };
  });
  console.log("卡在等输入", JSON.stringify(runState));

  // 按住 3.3 秒
  const box = await (await page.$("#btn-stop")).boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(3300);
  await page.mouse.up();
  await page.waitForTimeout(1200);

  const after = await page.evaluate(async () => {
    const d = await (await fetch("/api/state")).json();
    return { phase: d.phase, snap: d.snapshot && d.snapshot.state,
             badge: document.getElementById("status-badge").textContent,
             fill: document.querySelector("#btn-stop .hold-fill").style.width };
  });
  console.log("长按之后 ", JSON.stringify(after));

  // 强杀之后还能不能再调试
  await page.click("#btn-debug");
  await page.waitForTimeout(1500);
  console.log("再点调试 ", JSON.stringify(await page.evaluate(async () => {
    const d = await (await fetch("/api/state")).json();
    return { phase: d.phase, snap: d.snapshot && d.snapshot.state, line: d.snapshot && d.snapshot.current_line };
  })));

  cmds.forEach((c) => console.log("  " + c));
  console.log(errors.length ? "页面错误:\n" + errors.join("\n") : "无页面错误");
  await browser.close();
})().catch((e) => { console.error("FAILED:", e.message); process.exit(1); });
