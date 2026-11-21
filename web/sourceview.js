/* ============================================================
   sourceview.js —— 源码面板
   负责：渲染代码、当前行高亮、断点标记、点击设断点。
   ============================================================ */

const SourceView = (() => {

  let lines = [];
  let currentLine = 0;
  let breakpoints = new Set();
  let errors = new Map();   // 行号 -> 错误消息
  let onToggleBreak = null;
  let scrollTop = 0;
  let cursorLine = 0;       // 用户点选/光标所在行（供「运行到光标」用）

  function setErrors(list) {
    errors = new Map();
    (list || []).forEach((e) => {
      if (e && e.line) errors.set(e.line, e);
    });
    if (lines.length) {
      // 只重画行样式，不重置滚动位置
      render(lines, null, true);
    }
  }

  function render(sourceLines, snap, keepScroll) {
    const prevScroll = keepScroll ? scrollTop : 0;
    lines = sourceLines || [];
    currentLine = snap ? (snap.current_line || 0) : (currentLine || 0);

    const host = document.getElementById("source-code");
    host.innerHTML = "";

    const frag = document.createDocumentFragment();
    for (let i = 0; i < lines.length; i++) {
      const no = i + 1;
      const li = document.createElement("li");
      li.className = "src-line" + (no === currentLine ? " current" : "")
        + (no === cursorLine ? " cursor" : "");
      li.dataset.line = String(no);

      const err = errors.get(no);
      if (err) {
        li.classList.add("has-error");
        li.title = `第 ${no} 行：${err.message || "编译错误"}`;
      }

      const gutter = document.createElement("span");
      gutter.className = "src-gutter" + (breakpoints.has(no) ? " has-bp" : "");
      gutter.textContent = String(no);
      gutter.title = "点击设置/取消断点";
      gutter.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleBreak(no);
      });

      const text = document.createElement("span");
      text.className = "src-text";
      text.textContent = lines[i] === undefined ? "" : lines[i];

      // 点代码正文 = 移动光标（Dev-C++ 的「运行到光标」靠的就是这个）
      li.addEventListener("click", () => setCursor(no));

      li.appendChild(gutter);
      li.appendChild(text);
      frag.appendChild(li);
    }
    host.appendChild(frag);

    if (keepScroll) {
      const scroller = document.getElementById("source-scroll");
      if (scroller) scroller.scrollTop = prevScroll;
    } else {
      scrollToCurrent();
    }
  }

  function setCursor(line) {
    cursorLine = line;
    document.querySelectorAll(".src-line.cursor").forEach((el) => {
      el.classList.remove("cursor");
    });
    const el = document.querySelector(`.src-line[data-line="${line}"]`);
    if (el) el.classList.add("cursor");
  }

  function getCursorLine() {
    return cursorLine || currentLine || 0;
  }

  /* 清空界面时要把「停在哪 / 刚才点了哪」一起忘掉 ——
     render(source, null) 会刻意保留上一次的 currentLine（重画源码时
     不希望高亮乱跳），但清空是例外，得显式抹掉。 */
  function clearPosition() {
    currentLine = 0;
    cursorLine = 0;
    scrollTop = 0;
  }

  /* 从后端同步断点集合（重启后 GDB 会重建断点，界面要跟上） */
  function setBreakpoints(list) {
    breakpoints = new Set(list || []);
    document.querySelectorAll(".src-line").forEach((li) => {
      const no = parseInt(li.dataset.line, 10);
      const gutter = li.querySelector(".src-gutter");
      if (gutter) gutter.classList.toggle("has-bp", breakpoints.has(no));
    });
  }

  /* GDB 会把断点吸附到「这一行之后第一个有代码的行」上（空行、纯声明行
     都会被跳过）。点下去的那一行和真正落点往往差一两行 —— 界面必须跟着挪，
     否则断点标记和程序实际停的位置对不上，看起来就是「错层」。 */
  function retargetBreakpoint(from, to) {
    if (!from || !to || from === to) return;
    breakpoints.delete(from);
    breakpoints.add(to);
    document.querySelectorAll(`.src-line[data-line="${from}"], .src-line[data-line="${to}"]`)
      .forEach((li) => {
        const no = parseInt(li.dataset.line, 10);
        const gutter = li.querySelector(".src-gutter");
        if (gutter) gutter.classList.toggle("has-bp", breakpoints.has(no));
      });
  }

  function trackScroll() {
    const scroller = document.getElementById("source-scroll");
    if (scroller) scrollTop = scroller.scrollTop;
  }
  function toggleBreak(line) {
    if (breakpoints.has(line)) breakpoints.delete(line);
    else breakpoints.add(line);
    const gutter = document.querySelector(`.src-line[data-line="${line}"] .src-gutter`);
    if (gutter) gutter.classList.toggle("has-bp", breakpoints.has(line));
    if (onToggleBreak) onToggleBreak(line, breakpoints.has(line));
  }

  function scrollToCurrent() {
    if (!currentLine) return;
    const el = document.querySelector(`.src-line[data-line="${currentLine}"]`);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function setOnToggleBreak(fn) { onToggleBreak = fn; }

  return {
    render, setOnToggleBreak, setErrors, trackScroll,
    setCursor, getCursorLine, setBreakpoints, retargetBreakpoint,
    clearPosition,
  };
})();
