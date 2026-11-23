/* ============================================================
   app.js —— 主逻辑：状态管理、命令发送、渲染调度
   ============================================================ */

(function () {

  const state = {
    snapshot: null,
    source: [],
    program: "",
    running: false,
    busy: false,
    phase: "not-started",   // not-started | running | stopped | exited
    breakpoints: [],
    //: 命令超时没等到程序停下 —— 多半是卡在 scanf 上等输入。
    //  这个状态要一直亮着，直到程序真的动了一下（快照 seq 变了）。
    waiting: false,
    lastSeq: 0,
  };

  /* ---------------------------------------------------------
     与后端通信
     --------------------------------------------------------- */

  async function fetchState() {
    try {
      const resp = await fetch("/api/state");
      const data = await resp.json();
      if (data.ok) {
        state.source = data.source || [];
        state.program = data.program || "";
        state.running = data.running;
        state.phase = data.phase || "not-started";
        applyBuildErrors(data.build_errors || []);
        if (data.breakpoints) {
          state.breakpoints = data.breakpoints;
          SourceView.setBreakpoints(data.breakpoints);
        }
        if (data.snapshot) {
          applySnapshot(data.snapshot);
        } else {
          // 后端说「什么都没有」（刚启动 / 刚清空界面）：画面也要跟着空，
          // 别留着上一次调试的蓝行和时间轴。
          clearViewUI(state.phase);
        }
        updateChrome();
      }
    } catch (e) {
      console.error("state fetch failed", e);
    }
  }

  function applyBuildErrors(list) {
    const bar = document.getElementById("build-errors");
    const box = document.getElementById("build-errors-list");
    if (!bar || !box) return;

    if (!list || !list.length) {
      bar.hidden = true;
      box.innerHTML = "";
      SourceView.setErrors([]);
      return;
    }

    box.innerHTML = list.map((e) => `
      <div class="be-item ${e.level === "warning" ? "warn" : ""}">
        <span class="be-line">第 ${e.line} 行</span>
        <span class="be-msg">${escapeHtml(e.message || "")}</span>
      </div>
    `).join("");
    bar.hidden = false;
    SourceView.setErrors(list);
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* 这几条命令不能被「上一条命令还在跑」挡住。

     程序卡在 scanf 里时，界面正在等一条永远等不到的命令（比如在系统函数里
     点了「跳出函数」），这时停止 / 重启是唯一的出路 —— 必须立刻发出去，
     否则用户看到的就是「什么都点不动，只能关掉重来」。 */
  const URGENT_CMDS = { "stop": 1, "force-kill": 1, "restart": 1 };

  async function sendCommand(cmd, extra) {
    const urgent = !!URGENT_CMDS[cmd];
    // 上一条命令还没回来就丢掉这条 —— 但界面可能已经先改了样子
    // （断点点下去就立刻变色），所以要拉一次真实状态把手抖修正回来，
    // 否则界面和 GDB 会各说各话。
    // 救命命令例外：不排队、不锁按钮。
    if (state.busy && !urgent) { setTimeout(fetchState, 60); return; }
    if (!urgent) {
      state.busy = true;
      setControlsDisabled(true);
    }
    updateBusyHint();
    try {
      const resp = await fetch("/api/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ cmd: cmd }, extra || {})),
      });
      const data = await resp.json();
      if (!data.ok && data.message) {
        setBadge(data.message, "warn");
      }
      // 清空界面：后端已经把快照扔掉、阶段退回「未执行」，
      // 这里把画面同步擦干净（时间轴一起清 —— 它是前端自己的历史）。
      if (data.ok && data.cleared) {
        clearViewUI(data.phase);
        if (data.message) setBadge(data.message, "info");
      }
      // 断点会被 GDB 吸附到别处（空行 / 纯声明行没有代码），
      // 「运行到光标」也可能停在下一行 —— 这里把标记挪到真实落点，
      // 否则标记和程序真正停的位置会差一行。
      if (data.ok && data.line) {
        if (cmd === "break" && extra && extra.line) {
          SourceView.retargetBreakpoint(extra.line, data.line);
          if (data.message) setBadge(data.message, "info");
        }
        if (cmd === "goto") SourceView.setCursor(data.line);
      }
      // 命令等超时了也没等到程序停下来 —— 大概卡在等输入。
      // 反过来，命令正常返回（含 stop / restart）就说明不用再提示了。
      state.waiting = !!data.waiting;
      if (data.ok && data.message) setBadge(data.message, "info");
    } catch (e) {
      console.error("command failed", e);
      setBadge("命令发送失败", "warn");
    } finally {
      if (!urgent) {
        state.busy = false;
        setControlsDisabled(false);
      }
      updateBusyHint();
      // 后端 SSE 会推送新状态；这里再兜底拉一次
      setTimeout(fetchState, 250);
    }
  }

  /* ---------------------------------------------------------
     SSE 订阅
     --------------------------------------------------------- */

  function subscribe() {
    const es = new EventSource("/api/events");
    es.onmessage = (ev) => {
      try {
        const snap = JSON.parse(ev.data);
        if (snap) {
          // SSE 会带上会话阶段（运行中 / 已停止 / 未执行…）
          if (snap.phase) state.phase = snap.phase;
          applySnapshot(snap);
          updateChrome();
        } else {
          fetchState();
        }
      } catch (e) {
        console.error("bad event", e);
      }
    };
    es.onerror = () => {
      // EventSource 会自动重连
    };
  }

  /* ---------------------------------------------------------
     渲染
     --------------------------------------------------------- */

  function applySnapshot(snap) {
    state.snapshot = snap;
    // 程序真的动了一下（有新的快照），「等待输入」的提示就可以撤了
    if (snap.seq && snap.seq !== state.lastSeq) {
      state.lastSeq = snap.seq;
      state.waiting = false;
    }
    SourceView.render(state.source, snap);
    MemView.render(snap);
    VarPanel.renderStack(snap);
    VarPanel.renderVars(snap);
    VarPanel.renderLessons(snap);
    Timeline.push(snap);
  }

  /* 把整个画面恢复到「什么都没跑过」的样子。

     为什么不在前端假装：画面是 /api/state + SSE 的投影，只擦 DOM 的话
     下一条命令的 fetchState 或刷一下页面就把旧快照请回来了（那才是「假清空」）。
     所以后端 `clear` 命令会把 snapshot 置空、阶段退回 not-started 并广播 null，
     这里只是照着这份「什么都没有」把各个视图渲染成空。
     源码和断点保留 —— 那是源码上的东西，不属于「这一次运行」。 */
  function clearViewUI(phase) {
    state.snapshot = null;
    state.lastSeq = 0;
    state.waiting = false;
    state.running = false;
    state.phase = phase || "not-started";
    SourceView.clearPosition();      // 蓝行 / 绿行都撤掉
    SourceView.render(state.source, null);
    MemView.render(null);
    VarPanel.renderStack(null);
    VarPanel.renderVars(null);
    VarPanel.renderLessons(null);
    Timeline.clear();                // 回溯历史是前端的，一并清
    updateChrome();
  }

  /* 「程序运行中」提示条 —— 顺便兼职「是不是卡住了」。

     一条命令超过 3 秒还没回来，或者程序跑起来 3 秒没有任何新快照，
     基本就是卡住了（scanf 等输入、死循环、系统函数里退不出来）。
     这时候把文案换成「长按停止执行强制终止」，并染成告警色 ——
     因为停止按钮这个时候是唯一能救命的东西，必须让人看见。

     普通命令几十毫秒就回来，所以延迟 400ms 再显示，避免每次点按钮闪一下。 */
  const HINT_SHOW_MS = 400;
  const HINT_STUCK_MS = 3000;
  let hintTimer = null;      // 「该不该露脸」的延迟
  let stuckTimer = null;     // 3 秒没动静 → 换文案
  let busySince = 0;

  function busyCondition() {
    return state.phase === "running" || state.busy || state.waiting;
  }

  function updateBusyHint() {
    const hint = document.getElementById("busy-hint");
    if (!hint) return;

    if (!busyCondition()) {
      busySince = 0;
      if (hintTimer) { clearTimeout(hintTimer); hintTimer = null; }
      if (stuckTimer) { clearTimeout(stuckTimer); stuckTimer = null; }
      hint.hidden = true;
      hint.classList.remove("stuck");
      return;
    }

    const now = performance.now();
    if (!busySince) busySince = now;
    const elapsed = now - busySince;

    if (elapsed >= HINT_SHOW_MS) {
      const stuck = elapsed >= HINT_STUCK_MS;
      hint.hidden = false;
      hint.textContent = stuck
        ? "卡住了？长按「停止执行」1 秒强制终止程序"
        : "程序运行中…";
      hint.classList.toggle("stuck", stuck);
    } else if (!hintTimer) {
      hintTimer = setTimeout(() => {
        hintTimer = null;
        updateBusyHint();
      }, HINT_SHOW_MS - elapsed);
    }

    if (elapsed < HINT_STUCK_MS && !stuckTimer) {
      stuckTimer = setTimeout(() => {
        stuckTimer = null;
        updateBusyHint();
      }, HINT_STUCK_MS - elapsed);
    }
  }

  function updateChrome() {
    const snap = state.snapshot;
    const loc = document.getElementById("status-loc");
    const badge = document.getElementById("status-badge");

    if (snap && snap.state !== "not-started") {
      const file = snap.current_file ? basename(snap.current_file) : "";
      loc.textContent = file
        ? `${file}:${snap.current_line}  ${snap.current_func || ""}`
        : "—";
    } else {
      loc.textContent = "—";
    }

    // 临时提示有「保质期」：没过期就先让它占着，别被常规刷新盖掉。
    // 否则 SSE 推一份快照过来，「已经在 main 里了…」只闪两百毫秒就没了。
    if (!badgeHold || performance.now() >= badgeHold.until) {
      badgeHold = null;
      badge.title = "";
      if (snap && snap.state === "exited") {
        badge.textContent = "程序已退出";
        badge.className = "badge exited";
      } else if (snap && snap.state === "not-started") {
        badge.textContent = "未执行";
        badge.className = "badge idle";
      } else if (state.phase === "running") {
        badge.textContent = "运行中";
        badge.className = "badge running";
      } else if (state.busy) {
        badge.textContent = "执行中…";
        badge.className = "badge running";
      } else {
        badge.textContent = "就绪";
        badge.className = "badge";
      }
    }

    document.getElementById("prog-name").textContent = state.program
      ? basename(state.program)
      : "—";

    updateBusyHint();
    updateButtonStates();
  }

  /* 各按钮的可用状态，取决于会话处在哪个阶段。

     这跟 Dev-C++ 的逻辑一致：
     - 未执行：只能「调试」和「重启」，别的执行控制都还没意义
     - 运行中：可以「停止执行」
     - 已停下：可以单步 / 继续 / 跳出 / 运行到光标

     唯一的例外是「停止执行」和「重启」：**永远可点**。
     程序卡在 scanf 里、GDB 卡在某个系统调用里，这两颗按钮是唯一的出路；
     一旦跟着别的按钮一起变灰，用户就只能去任务管理器收尸了。
  */
  function updateButtonStates() {
    const snap = state.snapshot;
    const phase = state.phase || "not-started";
    const notStarted = phase === "not-started";
    const running = phase === "running";
    const exited = phase === "exited" || (snap && snap.state === "exited");
    // 已经跑起来过（有过快照）才谈得上单步
    const hasSnapshot = !!snap && snap.state !== "not-started";

    const set = (id, enabled) => {
      const el = document.getElementById(id);
      if (el) el.disabled = !enabled;
    };

    set("btn-debug", !state.busy && !running);
    set("btn-next", !state.busy && hasSnapshot && !running && !exited);
    set("btn-step", !state.busy && hasSnapshot && !running && !exited);
    set("btn-finish", !state.busy && hasSnapshot && !running && !exited);
    set("btn-goto", !state.busy && hasSnapshot && !running && !exited);
    set("btn-continue", !state.busy && hasSnapshot && !running && !exited);
    // 这两颗永不禁用 —— 见上面的说明
    set("btn-stop", true);
    set("btn-restart", true);
    // 有画可清才亮：跑过、停在断点、已退出都算；刚打开或刚清空时是灰的
    set("btn-clear", !state.busy && (hasSnapshot || !notStarted));
  }

  /* 顶栏徽章上的临时提示（「已在第 31 行设置断点」「已经在 main 函数里了…」）。

     两个讲究：
     1. 有保质期（默认 2 秒）。updateChrome 随时会被 SSE 的快照刷新调用，
        不占位的话提示只活到下一帧 —— 用户根本来不及看完一句话。
     2. 同一句话正在显示时不再重设。用户连点几下「跳出函数」，计时不会一直
        被重置，文案也不会闪一下再回来。 */
  const BADGE_HOLD_MS = 2000;
  let badgeHold = null;

  function setBadge(text, kind, holdMs) {
    const badge = document.getElementById("status-badge");
    if (!text) return;
    const ms = holdMs || BADGE_HOLD_MS;
    const now = performance.now();
    if (badgeHold && badgeHold.text === text && now < badgeHold.until) return;
    badge.textContent = text;
    badge.className = "badge" + (kind === "warn" ? " running" : "");
    badge.title = text;              // 太长被省略号截了，悬停看全句
    badgeHold = { text: text, until: now + ms };
    setTimeout(updateChrome, ms + 30);
  }

  function setControlsDisabled(disabled) {
    // 真正的可用性判断交给 updateButtonStates，这里只管「命令进行中」。
    // 停止 / 重启刻意不在名单里：它们任何时候都得能点。
    ["btn-debug", "btn-next", "btn-step", "btn-finish", "btn-goto",
     "btn-continue", "btn-clear"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.disabled = disabled;
    });
  }

  function basename(p) {
    if (!p) return "";
    const parts = String(p).split(/[\\/]/);
    return parts[parts.length - 1];
  }

  /* ---------------------------------------------------------
     停止执行：短按 / 长按
     --------------------------------------------------------- */

  /* 正常停止走「中断 → GDB kill」，实测 0.2~0.4 秒就能确认死透，
     日常根本用不着别的。长按 1 秒这条是**兜底**：万一 GDB 自己卡住
     （程序陷在某个系统调用里、`console kill` 回 error），
     只有从外面强制结束进程（Windows 用 taskkill /F，Linux 用 SIGKILL），
     GDB 才会跟着醒过来。

     所以「短按」和「长按」都是停止，只差一刀砍在哪儿：
     手指抬起时不满 1 秒走普通 stop，按满 1 秒直接 force-kill。

     交互上刻意不做「长按才有反应」：按下就开始计时并画进度条，
     短按的手感和以前点击完全一致。 */
  function bindHoldStop() {
    const btn = document.getElementById("btn-stop");
    if (!btn) return;
    const HOLD_MS = 1000;
    const fill = btn.querySelector(".hold-fill");
    let raf = 0;
    let startedAt = 0;
    let holding = false;
    let fired = false;

    const paint = (p) => {
      if (fill) fill.style.width = Math.round(p * 100) + "%";
    };

    const reset = () => {
      holding = false;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      btn.classList.remove("holding");
      paint(0);
    };

    const begin = (ev) => {
      if (btn.disabled || (ev.button !== 0 && ev.pointerType === "mouse")) return;
      ev.preventDefault();          // 按住时不要顺手选中文字
      holding = true;
      fired = false;
      startedAt = performance.now();
      btn.classList.add("holding");
      paint(0);
      const tick = (now) => {
        if (!holding) return;
        const p = Math.min(1, (now - startedAt) / HOLD_MS);
        paint(p);
        if (p >= 1) {
          fired = true;
          reset();
          sendCommand("force-kill");
          return;
        }
        raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);
    };

    const end = () => {
      if (!holding) return;
      reset();
      if (fired) return;            // 已经在 tick 里发过强制结束了
      sendCommand("stop");
    };

    btn.addEventListener("pointerdown", begin);
    // 收尾挂在 window 上：手指按着拖出按钮再松开也要算数，
    // 否则进度条会一直挂在按钮上，看着像卡死了。
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    btn.addEventListener("contextmenu", (e) => e.preventDefault());
  }

  /* ---------------------------------------------------------
     事件绑定
     --------------------------------------------------------- */

  function currentCursorLine() {
    return SourceView.getCursorLine();
  }

  function bind() {
    document.getElementById("btn-debug").addEventListener("click", () => sendCommand("debug"));
    document.getElementById("btn-next").addEventListener("click", () => sendCommand("next"));
    document.getElementById("btn-step").addEventListener("click", () => sendCommand("step"));
    document.getElementById("btn-finish").addEventListener("click", () => sendCommand("finish"));
    document.getElementById("btn-goto").addEventListener("click", () => {
      sendCommand("goto", { line: currentCursorLine() });
    });
    document.getElementById("btn-continue").addEventListener("click", () => sendCommand("continue"));
    bindHoldStop();          // 停止执行：短按=停止，长按 1 秒=强制结束
    document.getElementById("btn-restart").addEventListener("click", () => sendCommand("restart"));
    document.getElementById("btn-clear").addEventListener("click", () => sendCommand("clear"));

    document.getElementById("mem-sort").addEventListener("change", () => {
      if (state.snapshot) MemView.render(state.snapshot);
    });

    // 地址显示方式：自动 / 紧凑 / 完整
    const addrSel = document.getElementById("addr-mode");
    if (addrSel) {
      addrSel.value = Addr.load();
      addrSel.addEventListener("change", () => {
        Addr.set(addrSel.value);
        if (state.snapshot) {
          MemView.render(state.snapshot);
          VarPanel.renderVars(state.snapshot);
        }
      });
    }

    document.getElementById("toggle-ascii").addEventListener("change", () => {
      if (state.snapshot) MemView.render(state.snapshot);
    });

    document.getElementById("btn-theme").addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme") || "dark";
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("cdbg-theme", next); } catch (e) { /* ignore */ }
    });

    SourceView.setOnToggleBreak((line, on) => {
      // 两个方向都要发命令。以前只发了「设置」，取消断点只改了本地的 Set，
      // GDB 那边压根没删 —— 于是下一条命令触发的状态同步又把红点刷回来了，
      // 看着就像「删了又自己长回来」。
      sendCommand("break", { file: state.program, line: line, on: !!on });
    });

    const srcScroll = document.getElementById("source-scroll");
    if (srcScroll) srcScroll.addEventListener("scroll", SourceView.trackScroll);

    // 键盘快捷键 —— 与 Dev-C++ 一致
    document.addEventListener("keydown", (e) => {
      if (e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
      switch (e.key) {
        case "F5":  e.preventDefault(); sendCommand("debug"); break;
        case "F7":  e.preventDefault(); sendCommand("next"); break;
        case "F8":  e.preventDefault(); sendCommand("step"); break;
        case "F4":  e.preventDefault(); sendCommand("continue"); break;
        case "F6":  e.preventDefault(); sendCommand("stop"); break;
        case "Ctrl+F8":  e.preventDefault(); sendCommand("finish"); break;
        case "Ctrl+F5":  e.preventDefault(); sendCommand("goto", { line: currentCursorLine() }); break;
        default: break;
      }
    });

    window.addEventListener("resize", () => {
      if (state.snapshot) requestAnimationFrame(() => MemView.render(state.snapshot));
    });

    document.getElementById("mem-scroll").addEventListener("scroll", () => {
      // 滚动后重画指针连线，让箭头跟着字节走
      requestAnimationFrame(() => MemView.redrawPointers());
    });
  }

  /* ---------------------------------------------------------
     底部那行小字（彩蛋）
     --------------------------------------------------------- */

  /* 一行「正在…」的废话，纯装饰，跟调试状态没有任何关系。

     几个约束：
     - 词库从后端拿一次（/api/quips），点按钮时在本地随机 —— 不为一行字发请求；
     - 每次随机都换掉上一条，否则连点两下没变化，看着像坏了；
     - 「隐藏」只管这一次：不写 localStorage，重新打开页面它还会回来。 */
  let quips = [];
  let quipCurrent = "";

  async function loadQuips() {
    try {
      const resp = await fetch("/api/quips");
      const data = await resp.json();
      if (!data || !data.ok || !data.lines || !data.lines.length) return;
      quips = data.lines;
      const bar = document.getElementById("quip-bar");
      if (bar) bar.hidden = false;
      nextQuip();
    } catch (e) {
      // 拿不到就算了，底下空着也不影响调试
    }
  }

  function nextQuip() {
    const text = document.getElementById("quip-text");
    if (!text || !quips.length) return;
    if (quips.length === 1) {
      text.textContent = quips[0];
      return;
    }
    let i = Math.floor(Math.random() * quips.length);
    // 撞上了就顺移一位 —— 比重新摇省事，而且保证一定换掉
    if (quips[i] === quipCurrent) i = (i + 1) % quips.length;
    quipCurrent = quips[i];
    text.textContent = quipCurrent;
  }

  function bindQuips() {
    // 点任何按钮都换一条。挂在捕获阶段：按钮自己的处理函数
    // 就算 stopPropagation 也挡不住这里。
    document.addEventListener("click", (ev) => {
      const el = ev.target;
      if (el && el.closest && el.closest("button")) nextQuip();
    }, true);

    const hide = document.getElementById("btn-quip-hide");
    if (hide) {
      hide.addEventListener("click", () => {
        const bar = document.getElementById("quip-bar");
        if (bar) bar.hidden = true;
      });
    }

    loadQuips();
  }

  /* ---------------------------------------------------------
     被调试程序的终端（Linux PTY）
     --------------------------------------------------------- */

  let consoleOffset = 0;
  let consoleTimer = 0;

  function setConsoleHint(mode) {
    const panel = document.getElementById("console-panel");
    const hint = document.getElementById("console-hint");
    const input = document.getElementById("console-input");
    const form = document.getElementById("console-form");

    // 这块面板只有 Linux 的 PTY 用得上 —— 那是被调试程序唯一的 stdin/stdout，
    // 在下面这行输入框里敲字，程序才吃得到。
    // Windows 上程序跑在自己的控制台窗口里，这里只剩一句「程序在独立控制台
    // 窗口中运行」加一片空白，白占半屏高度，所以收起来。
    //
    // 判定用后端报的 mode 而不是 UA：WSL 里 cdbg 跑在 Linux（pty），
    // 浏览器却是 Windows 的，按 UA 藏了就没地方输入了。
    // HTML 里默认就是 hidden —— 这里只负责「该出现的时候让它出现」。
    if (panel) panel.hidden = mode !== "pty";

    if (!hint) return;
    const writable = mode === "pty";
    if (input) input.disabled = !writable;
    if (form) form.classList.toggle("disabled", !writable);
    if (mode === "pty") {
      hint.textContent = "终端已连接 · 在这里输入程序需要的字符";
    } else if (mode === "own") {
      hint.textContent = "程序在独立控制台窗口中运行";
    } else if (mode === "shared") {
      hint.textContent = "程序与 GDB 共用控制台";
    } else {
      hint.textContent = "程序输出不可用（当前平台没有终端）";
    }
  }

  async function pollConsole() {
    const out = document.getElementById("console-output");
    if (!out) return;
    try {
      const resp = await fetch("/api/console?offset=" + consoleOffset);
      const data = await resp.json();
      if (!data.ok) return;
      // 会话重启后服务端日志会清零，offset 比本地小 —— 本地也清空重新来
      if (data.offset < consoleOffset) {
        out.textContent = "";
        consoleOffset = 0;
        return;
      }
      if (data.text) {
        out.textContent += data.text;
        // 防止长时间运行把浏览器拖垮，只保留最后 200KB
        if (out.textContent.length > 200000) {
          out.textContent = out.textContent.slice(-200000);
        }
        out.scrollTop = out.scrollHeight;
      }
      consoleOffset = data.offset;
      setConsoleHint(data.mode || "none");
    } catch (e) {
      // 服务没起来时安静等待下一轮
    }
  }

  function bindConsole() {
    const panel = document.getElementById("console-panel");
    const form = document.getElementById("console-form");
    const input = document.getElementById("console-input");
    const clear = document.getElementById("btn-console-clear");
    const toggle = document.getElementById("btn-console-toggle");
    if (!panel) return;

    try {
      if (localStorage.getItem("cdbg-console-collapsed") === "1") {
        panel.classList.add("collapsed");
        if (toggle) toggle.textContent = "展开";
      }
    } catch (e) { /* ignore */ }

    if (toggle) {
      toggle.addEventListener("click", () => {
        const collapsed = panel.classList.toggle("collapsed");
        toggle.textContent = collapsed ? "展开" : "收起";
        try {
          localStorage.setItem("cdbg-console-collapsed", collapsed ? "1" : "0");
        } catch (e) { /* ignore */ }
      });
    }

    if (clear) {
      clear.addEventListener("click", () => {
        const out = document.getElementById("console-output");
        if (out) out.textContent = "";
      });
    }

    if (form && input) {
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const value = input.value;
        input.value = "";
        if (!value) return;
        try {
          await fetch("/api/console", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ data: value + "\n" }),
          });
        } catch (e) { /* ignore */ }
        pollConsole();
      });
    }

    if (!consoleTimer) consoleTimer = setInterval(pollConsole, 600);
    pollConsole();
  }

  /* ---------------------------------------------------------
     启动
     --------------------------------------------------------- */

  function init() {
    try {
      const saved = localStorage.getItem("cdbg-theme");
      if (saved) document.documentElement.setAttribute("data-theme", saved);
    } catch (e) { /* ignore */ }

    bind();
    bindConsole();
    bindQuips();

    // 回看历史快照：只影响显示，不改变调试状态
    Timeline.setOnView((snap) => {
      state.viewing = snap;
      SourceView.render(state.source, snap);
      MemView.render(snap);
      VarPanel.renderStack(snap);
      VarPanel.renderVars(snap);
      VarPanel.renderLessons(snap);
    });
    Timeline.bind();

    fetchState();
    subscribe();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
