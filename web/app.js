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

  async function sendCommand(cmd, extra) {
    // 上一条命令还没回来就丢掉这条 —— 但界面可能已经先改了样子
    // （断点点下去就立刻变色），所以要拉一次真实状态把手抖修正回来，
    // 否则界面和 GDB 会各说各话。
    if (state.busy) { setTimeout(fetchState, 60); return; }
    state.busy = true;
    setControlsDisabled(true);
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
    } catch (e) {
      console.error("command failed", e);
      setBadge("命令发送失败", "warn");
    } finally {
      state.busy = false;
      setControlsDisabled(false);
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

  /* 「指令运行中，请检查程序是否在等待输入」——
     程序卡在 scanf 上时，前端只能干等到超时，按钮一直是灰的。
     这行字就是解释：不是坏了，是它在等你在控制台敲点东西。

     普通命令几十毫秒就回来，所以延迟 600ms 再显示，避免每次点按钮闪一下。 */
  let busyHintTimer = null;

  function updateBusyHint() {
    const hint = document.getElementById("busy-hint");
    if (!hint) return;
    const shouldShow = state.phase === "running" || state.busy || state.waiting;

    if (shouldShow) {
      if (hint.hidden && !busyHintTimer) {
        busyHintTimer = setTimeout(() => {
          busyHintTimer = null;
          hint.hidden = false;
        }, 600);
      }
      return;
    }
    clearTimeout(busyHintTimer);
    busyHintTimer = null;
    hint.hidden = true;
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
    set("btn-stop", !state.busy && (running || hasSnapshot) && !exited);
    set("btn-restart", !state.busy && !notStarted);
    // 有画可清才亮：跑过、停在断点、已退出都算；刚打开或刚清空时是灰的
    set("btn-clear", !state.busy && (hasSnapshot || !notStarted));
  }

  function setBadge(text, kind) {
    const badge = document.getElementById("status-badge");
    badge.textContent = text;
    badge.className = "badge" + (kind === "warn" ? " running" : "");
    setTimeout(updateChrome, 2200);
  }

  function setControlsDisabled(disabled) {
    // 真正的可用性判断交给 updateButtonStates，这里只管「命令进行中」
    ["btn-debug", "btn-next", "btn-step", "btn-finish", "btn-goto",
     "btn-continue", "btn-stop", "btn-restart", "btn-clear"].forEach((id) => {
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
     日常根本用不着别的。长按 3 秒这条是**兜底**：万一 GDB 自己卡住
     （程序陷在某个系统调用里、`console kill` 回 error），
     只有从外面 taskkill /F 把进程砍掉，GDB 才会跟着醒过来。

     交互上刻意不做「长按才有反应」：按下就开始计时，手指抬起时
     不满 3 秒就按普通「停止执行」处理 —— 和以前的点击手感完全一致，
     只是按住不放会多出一条进度条。 */
  function bindHoldStop() {
    const btn = document.getElementById("btn-stop");
    if (!btn) return;
    const HOLD_MS = 3000;
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
    bindHoldStop();          // 停止执行：短按=停止，长按 3 秒=强制结束
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
     启动
     --------------------------------------------------------- */

  function init() {
    try {
      const saved = localStorage.getItem("cdbg-theme");
      if (saved) document.documentElement.setAttribute("data-theme", saved);
    } catch (e) { /* ignore */ }

    bind();

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
