/* ============================================================
   timeline.js —— 快照时间轴与变量 diff
   ============================================================ */

const Timeline = (() => {

  const MAX = 200;
  let history = [];          // 快照历史
  let current = null;        // 当前显示的快照
  let compareWith = null;    // 对比基准
  let onView = null;         // 切换显示某个快照的回调

  function push(snap) {
    if (!snap) return;
    current = snap;
    // 同一个 seq 不重复记录
    if (history.length && history[history.length - 1].seq === snap.seq) {
      history[history.length - 1] = snap;
    } else {
      history.push(snap);
      if (history.length > MAX) history.shift();
    }
    render();
  }

  function render() {
    const track = document.getElementById("tl-track");
    if (!track) return;
    track.innerHTML = "";

    history.forEach((snap, i) => {
      const tick = document.createElement("span");
      tick.className = "tl-tick";
      tick.dataset.idx = String(i);

      const depth = snap.depth || 0;
      // 刻度高度反映调用栈深度 —— 递归变深时能一眼看出来
      tick.style.height = (6 + Math.min(depth, 8) * 4) + "px";
      if (depth > 1) tick.classList.add("recursive");

      const isCurrent = current && snap.seq === current.seq;
      if (isCurrent) tick.classList.add("active");
      if (compareWith && snap.seq === compareWith.seq) tick.classList.add("compare");

      tick.title = `#${snap.seq}  ${snap.current_func || ""}  行 ${snap.current_line}  深度 ${depth}`;
      track.appendChild(tick);
    });

    track.scrollLeft = track.scrollWidth;
    const hint = document.getElementById("tl-hint");
    if (hint) {
      hint.textContent = history.length
        ? `${history.length} 个快照 · 点击回看 · 双击对比`
        : "点击回看 · 双击与当前对比";
    }
  }

  function viewAt(idx) {
    if (idx < 0 || idx >= history.length) return;
    const snap = history[idx];
    current = snap;
    render();
    if (onView) onView(snap);
  }

  /* ---------------------------------------------------------
     变量 diff
     --------------------------------------------------------- */

  function varMap(snap) {
    const map = new Map();
    (snap.globals || []).forEach((v) => map.set("g:" + v.name, v));
    (snap.frames || []).forEach((fr) => {
      (fr.vars || []).forEach((v) => map.set(`f${fr.index}:${v.name}`, v));
    });
    return map;
  }

  function showDiff(base, head) {
    const panel = document.getElementById("diff-panel");
    const body = document.getElementById("diff-body");
    const title = document.getElementById("diff-title");
    if (!panel || !body) return;

    const a = varMap(base);
    const b = varMap(head);
    const rows = [];

    for (const [key, bv] of b) {
      const av = a.get(key);
      if (!av) {
        rows.push({ kind: "new", key: key, name: bv.name, from: "—", to: bv.value });
      } else if (String(av.value) !== String(bv.value)) {
        rows.push({ kind: "changed", key: key, name: bv.name, from: av.value, to: bv.value });
      }
    }
    for (const [key, av] of a) {
      if (!b.has(key)) {
        rows.push({ kind: "gone", key: key, name: av.name, from: av.value, to: "—" });
      }
    }

    title.textContent = `变量变化：#${base.seq} → #${head.seq}`;

    if (!rows.length) {
      body.innerHTML = '<div class="diff-empty">这两个快照之间没有变量变化</div>';
    } else {
      body.innerHTML = rows.map((r) => `
        <div class="diff-row ${r.kind}">
          <span class="diff-name">${escapeHtml(r.name)}</span>
          <span class="diff-from">${escapeHtml(String(r.from))}</span>
          <span class="diff-arrow">→</span>
          <span class="diff-to">${escapeHtml(String(r.to))}</span>
        </div>
      `).join("");
    }
    panel.hidden = false;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function bind() {
    const track = document.getElementById("tl-track");
    if (!track) return;

    track.addEventListener("click", (e) => {
      const tick = e.target.closest(".tl-tick");
      if (!tick) return;
      viewAt(Number(tick.dataset.idx));
    });

    track.addEventListener("dblclick", (e) => {
      const tick = e.target.closest(".tl-tick");
      if (!tick) return;
      const idx = Number(tick.dataset.idx);
      const base = history[idx];
      if (!base || !current) return;
      compareWith = base;
      render();
      showDiff(base, current);
    });

    const closeBtn = document.getElementById("btn-close-diff");
    if (closeBtn) {
      closeBtn.addEventListener("click", () => {
        document.getElementById("diff-panel").hidden = true;
        compareWith = null;
        render();
      });
    }

    const clearBtn = document.getElementById("btn-clear-tl");
    if (clearBtn) {
      clearBtn.addEventListener("click", () => {
        history = [];
        compareWith = null;
        document.getElementById("diff-panel").hidden = true;
        render();
      });
    }
  }

  function setOnView(fn) { onView = fn; }

  function clear() {
    history = [];
    current = null;
    compareWith = null;
    const panel = document.getElementById("diff-panel");
    if (panel) panel.hidden = true;
    render();
  }

  return { push, bind, setOnView, clear, showDiff };
})();
