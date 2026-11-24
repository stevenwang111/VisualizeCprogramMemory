/* ============================================================
   varpanel.js —— 调用栈、变量卡片、教学提示
   ============================================================ */

const VarPanel = (() => {

  // 地址统一交给 Addr 模块格式化
  function hex16(n) { return Addr.format(n); }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function classifyType(v) {
    if (!v) return "unknown";
    if (v.is_pointer) return "ptr";
    if (v.array_length) return "array";
    if (v.is_string) return "string";
    const t = (v.type || "").toLowerCase();
    if (t.includes("char")) return "char";
    if (t.includes("float") || t.includes("double")) return "float";
    if (t.includes("struct") || t.includes("union") || t.includes("{")) return "struct";
    if (t.includes("int") || t.includes("long") || t.includes("short") || t.includes("unsigned")) return "int";
    return "unknown";
  }

  /* ---------------------------------------------------------
     调用栈
     --------------------------------------------------------- */

  function renderStack(snap) {
    const host = document.getElementById("stack-list");
    host.innerHTML = "";

    const frames = (snap && snap.frames) || [];
    if (!snap) {
      host.innerHTML = '<div class="var-group-title">（尚未启动）</div>';
      return;
    }
    const hint = document.getElementById("stack-hint");

    if (frames.length === 0) {
      host.innerHTML = '<div class="var-group-title">（无调用栈信息）</div>';
      hint.textContent = "—";
      return;
    }

    hint.textContent = `深度 ${frames.length} · 栈占用 ${snap.stack_bytes || 0} 字节`;

    // 统计同名函数出现次数 —— 用于标注递归
    const nameCount = {};
    frames.forEach((fr) => {
      nameCount[fr.func] = (nameCount[fr.func] || 0) + 1;
    });

    for (const fr of frames) {
      const el = document.createElement("div");
      el.className = "stack-item" + (fr.index === 0 ? " current" : "");
      el.style.marginLeft = (fr.index * 6) + "px";
      el.style.borderLeftColor = frameHueCss(fr.index);

      const loc = fr.file ? `${basename(fr.file)}:${fr.line}` : "?";
      const recursive = nameCount[fr.func] > 1 ? '<span class="stack-rec">递归</span>' : "";

      el.innerHTML = `
        <span class="stack-idx">#${fr.index}</span>
        <span class="stack-func">${esc(fr.func)}()</span>
        ${recursive}
        <span class="stack-size">${fr.size || 0}B</span>
        <span class="stack-loc">${esc(loc)}</span>
      `;

      el.addEventListener("click", () => {
        const v = (fr.vars || [])[0];
        if (v && v.address != null) MemView.focusAddress(v.address);
      });

      host.appendChild(el);
    }
  }

  const FRAME_HUES = [205, 145, 320, 35, 265, 95, 175, 300, 15, 235];

  function frameHueCss(index) {
    const hue = FRAME_HUES[index % FRAME_HUES.length];
    return `hsl(${hue} 62% 58%)`;
  }

  /* ---------------------------------------------------------
     变量
     --------------------------------------------------------- */

  function renderVars(snap) {
    const host = document.getElementById("var-list");
    host.innerHTML = "";
    const hint = document.getElementById("var-hint");

    if (!snap) {
      host.innerHTML = '<div class="var-group-title">（尚未启动）</div>';
      hint.textContent = "—";
      return;
    }
    const frames = snap.frames || [];
    const globals = snap.globals || [];
    const notStarted = snap.state === "not-started";

    let count = 0;

    // 全局与静态变量
    if (globals.length) {
      host.appendChild(groupTitle(
        notStarted ? "全局 / 静态变量（程序尚未执行，值为初始值）" : "全局 / 静态变量"));
      globals.forEach((v) => { host.appendChild(varCard(v)); count++; });
    }

    // 未执行时没有栈帧，说明一下，免得以为漏了内容
    if (notStarted && !frames.length) {
      host.appendChild(groupTitle("局部变量"));
      const el = document.createElement("div");
      el.className = "var-group-title";
      el.style.fontWeight = "normal";
      el.textContent = "（程序尚未执行，没有栈帧）";
      host.appendChild(el);
    }

    // 堆块
    const heaps = snap.heap || [];
    if (heaps.length) {
      host.appendChild(groupTitle(
        `堆（${heaps.length} 块 · 共 ${snap.heap_total || 0} 字节）`));
      heaps.forEach((h) => {
        const el = document.createElement("div");
        el.className = "var-card k-heap";
        el.innerHTML = `
          <div class="var-head">
            <span class="var-name">${hex16(h.address)}</span>
            <span class="var-type">${h.size || "?"} 字节</span>
            <span class="var-addr">${esc(h.site || "")}</span>
          </div>
          <div class="var-value">${esc(h.label || "")}</div>
        `;
        el.addEventListener("click", () => MemView.focusAddress(h.address));
        host.appendChild(el);
        count++;
      });
    }

    // 各栈帧的局部变量
    for (const fr of frames) {
      const vars = fr.vars || [];
      if (!vars.length) continue;
      const label = fr.index === 0
        ? `当前函数 ${fr.func}()`
        : `第 ${fr.index} 层 ${fr.func}()`;
      host.appendChild(groupTitle(label));
      vars.forEach((v) => { host.appendChild(varCard(v)); count++; });
    }

    if (!count) {
      host.innerHTML = '<div class="var-group-title">（没有采集到变量）</div>';
      hint.textContent = "—";
    } else {
      hint.textContent = `${count} 个`;
    }
  }

  function groupTitle(text) {
    const el = document.createElement("div");
    el.className = "var-group-title";
    el.textContent = text;
    return el;
  }

  function varCard(v) {
    const kind = classifyType(v);
    const el = document.createElement("div");
    el.className = "var-card k-" + kind;

    const valHtml = valueHtml(v);

    // static 变量单独标出来：它住在 .data / .bss，不在栈上，所以永远躺在
    // 「全局 / 静态」那一组里 —— 不标的话，用户会以为这是当前函数的局部变量
    // 混进了全局区。
    const staticBadge = v.scope === "static"
      ? `<span class="var-badge static" title="${v.decl_func
          ? esc(v.decl_func) + "() 里声明的 static：不在栈上，整个程序运行期间都活着"
          : "静态存储：不在栈上，整个程序运行期间都活着"}">${v.decl_func
          ? esc(v.decl_func) + " 内静态"
          : "静态"}</span>`
      : "";

    // 变量住在哪个段：卡片上直接写 `.bss` / `.data`，这是事实；
    // 「有没有初始化」是推断，不写。用户看到「值 12 但地址在 .bss」时，
    // 至少能确认自己看的是哪一段，而不是以为采集错了段。
    const segName = { data: ".data", bss: ".bss", rdata: ".rdata" }[v.storage];
    const segTag = (segName && (v.scope === "global" || v.scope === "static"))
      ? `<span class="var-seg" title="这个变量住在 ${segName} 段">${segName}</span>`
      : "";

    let html = `
      <div class="var-head">
        <span class="var-name">${esc(v.name)}</span>
        <span class="var-type">${esc(v.type || "?")}${v.array_length ? " [" + v.array_length + "]" : ""}</span>
        ${segTag}
        ${staticBadge}
        ${v.initialized === false ? '<span class="var-badge uninit">未初始化</span>' : ""}
        <span class="var-addr">${v.address != null ? hex16(v.address) : "—"} · ${v.size || 0}B</span>
      </div>
      <div class="var-value">${valHtml}</div>
    `;

    if (v.is_pointer && v.pointer_target != null) {
      html += `<div class="var-arrow">
        <span class="arrow">指向 →</span>
        <span class="target">${hex16(v.pointer_target)}</span>
        ${v.pointer_target_name ? `<span class="arrow">(${esc(v.pointer_target_name)})</span>` : ""}
      </div>`;
    }

    if (v.issues && v.issues.length) {
      html += `<div class="var-issue">⚠ ${esc(v.issues.join("；"))}</div>`;
    }

    el.innerHTML = html;
    el.addEventListener("click", () => {
      if (v.address != null) MemView.focusAddress(v.address);
    });
    return el;
  }

  function valueHtml(v) {
    if (v.is_string && v.preview != null) {
      return `<span class="str">"${esc(v.preview)}"</span>`;
    }
    if (v.is_pointer) {
      // 指针的值就是它存的地址 —— 用同一套地址格式渲染，
      // 免得变量卡片里是一长串 0、地址列却是紧凑形式，看着不对齐。
      if (v.pointer_target != null) {
        return `<span class="ptr">${esc(Addr.format(v.pointer_target))}</span>`;
      }
      return `<span class="ptr">${esc(v.value || "")}</span>`;
    }
    return esc(v.value || "");
  }

  /* ---------------------------------------------------------
     教学提示
     --------------------------------------------------------- */

  function renderLessons(snap) {
    const host = document.getElementById("lesson-list");
    host.innerHTML = "";

    const lessons = (snap && snap.lessons) || [];
    if (!snap) {
      host.innerHTML = '<div class="var-group-title">（编译通过后这里会显示对应的教材知识点）</div>';
      return;
    }
    if (!lessons.length) {
      host.innerHTML = '<div class="var-group-title">（当前没有匹配的知识点提示）</div>';
      return;
    }

    for (const lesson of lessons) {
      const el = document.createElement("div");
      el.className = "lesson" + (lesson.level === "warn" ? " warn" : "");
      el.innerHTML = `
        <div class="lesson-head">
          <span class="lesson-chapter">${esc(lesson.chapter)}</span>
          <span class="lesson-topic">${esc(lesson.topic)}</span>
        </div>
        <div class="lesson-msg">${bold(esc(lesson.message))}</div>
      `;
      host.appendChild(el);
    }
  }

  /** 文案里用 **…** 标重点；直接当纯文本渲染会把星号露出来。 */
  function bold(text) {
    return String(text).replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  }

  function basename(p) {
    if (!p) return "?";
    const parts = String(p).split(/[\\/]/);
    return parts[parts.length - 1];
  }

  return { renderStack, renderVars, renderLessons };
})();
