/* ============================================================
   splitter.js —— 三分栏宽度可拖动

   布局是 grid，五列：源码 | 分隔条 | 内存 | 分隔条 | 变量。
   拖动分隔条时直接改写 grid-template-columns 的像素值，
   视觉边界和实际宽度永远一致，不需要量位置去对齐。

   宽度以「比例」保存在 localStorage，窗口缩放时按比例重算，
   所以拉大窗口不会把某一栏挤成一条缝。
   ============================================================ */

const SplitLayout = (() => {

  const KEY = "cdbg-cols";
  // 默认 26% / 52% / 22%：源码行不长，中间的内存表最吃宽度，
  // 右边变量卡片够放地址就行。
  const DEFAULT_RATIOS = [0.26, 0.52, 0.22];
  const SPLIT_W = 5;     // 分隔条占的列宽，必须和 theme.css 里的 5px 一致
  const GAPS = 4;        // 五列之间 4 个 1px 的 gap
  const NARROW = 1100;   // 与 theme.css 的断点保持一致
  const MIN = [200, 280, 200];   // 三栏的最小宽度

  const panels = [];
  let grid = null;
  let ratios = DEFAULT_RATIOS.slice();
  let drag = null;

  /* ---------------- 存取 ---------------- */

  function load() {
    try {
      const raw = localStorage.getItem(KEY);
      if (!raw) return;
      const v = JSON.parse(raw);
      if (!Array.isArray(v) || v.length !== 3) return;
      if (!v.every((n) => typeof n === "number" && isFinite(n) && n > 0.01)) return;
      const sum = v[0] + v[1] + v[2];
      ratios = v.map((n) => n / sum);
    } catch (e) { /* localStorage 不可用就用默认值 */ }
  }

  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(ratios)); } catch (e) { /* ignore */ }
  }

  /* ---------------- 宽度计算 ---------------- */

  function availWidth() {
    return grid.clientWidth - 2 * SPLIT_W - GAPS;
  }

  /** 把三栏压到最小宽度以上；不够就把差额按「富余量」摊给其它栏。 */
  function clampWidths(w, avail) {
    if (avail <= MIN[0] + MIN[1] + MIN[2]) {
      // 窄到无法满足最小宽度：按最小宽度之比压缩，至少不变形
      const m = MIN[0] + MIN[1] + MIN[2];
      return MIN.map((v) => Math.round((v / m) * avail));
    }
    const out = w.slice();
    for (let pass = 0; pass < 3; pass++) {
      let deficit = 0;
      for (let i = 0; i < 3; i++) {
        if (out[i] < MIN[i]) { deficit += MIN[i] - out[i]; out[i] = MIN[i]; }
      }
      if (deficit <= 0) break;
      const slack = out.map((x, i) => Math.max(0, x - MIN[i]));
      const totalSlack = slack[0] + slack[1] + slack[2];
      if (totalSlack <= 0) break;
      for (let i = 0; i < 3; i++) {
        out[i] -= Math.round((slack[i] / totalSlack) * deficit);
      }
    }
    // 取整误差归到最后一栏，保证总和精确
    out[2] = avail - out[0] - out[1];
    return out;
  }

  function setWidths(w) {
    const avail = availWidth();
    if (avail <= 0) return;
    const c = clampWidths(w, avail);
    grid.style.gridTemplateColumns =
      `${c[0]}px ${SPLIT_W}px ${c[1]}px ${SPLIT_W}px ${c[2]}px`;
    ratios = [c[0] / avail, c[1] / avail, c[2] / avail];
  }

  function apply() {
    if (!grid) return;
    // 窄屏走 CSS 的两栏排布，行内宽度必须让开，否则会压过媒体查询
    if (grid.clientWidth < NARROW) {
      grid.style.gridTemplateColumns = "";
      return;
    }
    const avail = availWidth();
    if (avail <= 0) return;
    setWidths(ratios.map((r) => r * avail));
  }

  function currentWidths() {
    return panels.map((p) => p.getBoundingClientRect().width);
  }

  function redrawPointers() {
    // memview.js 里 MemView 是 const 声明的，**不会**挂到 window 上，
    // 所以不能写 window.MemView —— 那样判断永远为假，拖完线不刷新。
    if (typeof MemView !== "undefined" && MemView.redrawPointers) {
      MemView.redrawPointers();
    }
  }

  /* ---------------- 拖动 ---------------- */

  function onMove(ev) {
    if (!drag) return;
    const dx = ev.clientX - drag.x;
    const w = drag.w.slice();
    if (drag.idx === 0) { w[0] += dx; w[1] -= dx; }
    else { w[1] += dx; w[2] -= dx; }
    setWidths(w);
    if (!drag.raf) {
      drag.raf = requestAnimationFrame(() => { drag.raf = 0; redrawPointers(); });
    }
  }

  function onUp() {
    if (!drag) return;
    const el = drag.el;
    el.classList.remove("dragging");
    document.body.classList.remove("col-resizing");
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    drag = null;
    save();
    redrawPointers();
  }

  function bind(el, idx) {
    if (!el) return;
    el.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0) return;
      drag = { idx, x: ev.clientX, w: currentWidths(), el, raf: 0 };
      el.classList.add("dragging");
      document.body.classList.add("col-resizing");
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      ev.preventDefault();
    });
    // 双击还原 —— 拖歪了不用去翻 localStorage
    el.addEventListener("dblclick", () => {
      ratios = DEFAULT_RATIOS.slice();
      apply();
      save();
      redrawPointers();
    });
  }

  function init() {
    grid = document.querySelector(".layout");
    if (!grid) return;
    document
      .querySelectorAll(".panel-source, .panel-mem, .panel-side")
      .forEach((p) => panels.push(p));

    load();
    bind(document.getElementById("split-1"), 0);
    bind(document.getElementById("split-2"), 1);
    apply();

    window.addEventListener("resize", () => {
      apply();
      requestAnimationFrame(redrawPointers);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  return { reset: () => { ratios = DEFAULT_RATIOS.slice(); apply(); save(); }, apply };
})();
