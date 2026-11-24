/* ============================================================
   memview.js —— 内存视图渲染

   负责：四分区渲染、HEX 网格、变量着色、指针指向高亮与连线。
   ============================================================ */

const MemView = (() => {

  const KIND_ORDER = ["text", "data", "rdata", "bss", "heap", "stack"];

  const KIND_COLOR = {
    text:   "#8b95a7",
    data:   "#58a6ff",
    rdata:  "#7ee787",
    bss:    "#4ecdc4",
    heap:   "#d2a8ff",
    stack:  "#ff9f43",
  };

  const KIND_NAME = {
    text:   "程序段",
    data:   "全局/静态（初值非 0）",
    rdata:  "只读数据",
    bss:    "全局/静态（初值全 0）",
    heap:   "堆",
    stack:  "栈",
  };

  const TYPE_COLOR = {
    int:    "var(--v-int)",
    char:   "var(--v-char)",
    float:  "var(--v-float)",
    ptr:    "var(--v-ptr)",
    struct: "var(--v-struct)",
    array:  "var(--v-array)",
    string: "var(--v-string)",
    unknown:"var(--v-unknown)",
  };

  /* 递归栈可视化：每层栈帧用独立色相区分 */
  const FRAME_HUES = [205, 145, 320, 35, 265, 95, 175, 300, 15, 235];

  function frameHue(index) {
    return FRAME_HUES[index % FRAME_HUES.length];
  }

  function frameColor(index, alpha) {
    return "hsl(" + frameHue(index) + " 62% " + (index === 0 ? "58%" : "48%") + " / " + (alpha || 0.22) + ")";
  }

  // 每个区域最多渲染多少字节，避免 .text / 堆把页面撑爆
  const RENDER_LIMIT = { text: 192, heap: 1024, stack: 4096, data: 4096, rdata: 4096, bss: 4096 };

  // 指针箭头最多跨几行。再远（或者跨分区）就不画线了 —— 那种线要竖着穿过
  // 好几个分区标题和十几行字节，看着像渲染错乱，改成「→ 变量名」徽标更清楚。
  const MAX_ARROW_ROWS = 6;

  let snapshot = null;
  let index = null;      // 地址 -> 变量信息
  let pointerTargets = new Set();
  let onFocus = null;

  /* ---------------------------------------------------------
     工具
     --------------------------------------------------------- */

  // 地址统一交给 Addr 模块格式化（可自动折叠高 32 位为 0 的部分）
  function hex16(n) { return Addr.format(n); }
  function hex8(n) { return Addr.format(n); }

  function classifyType(v) {
    if (!v) return "unknown";
    if (v.is_pointer) return "ptr";
    if (v.array_length) return "array";
    if (v.is_string) return "string";
    const t = (v.type || "").toLowerCase();
    if (t.includes("char")) return "char";
    if (t.includes("float") || t.includes("double") || t.includes("long double")) return "float";
    if (t.includes("struct") || t.includes("union") || t.includes("{")) return "struct";
    if (t.includes("int") || t.includes("long") || t.includes("short") || t.includes("unsigned")) return "int";
    return "unknown";
  }

  function shortType(t) {
    if (!t) return "?";
    return t.length > 26 ? t.slice(0, 25) + "…" : t;
  }

  /* ---------------------------------------------------------
     建立变量索引：地址 -> 变量
     --------------------------------------------------------- */

  function buildIndex(snap) {
    const map = new Map();
    pointerTargets = new Set();
    if (!snap) return map;

    const register = (v) => {
      if (!v || v.address == null || !v.size) return;
      map.set(v.address, {
        address: v.address,
        name: v.name,
        size: v.size,
        type: v.type,
        kind: classifyType(v),
        scope: v.scope,
        frame: v.frame,
        value: v.value,
        isPointer: v.is_pointer,
        pointerTarget: v.pointer_target,
        preview: v.preview,
        issues: v.issues || [],
      });
      if (v.is_pointer && v.pointer_target != null) {
        pointerTargets.add(v.pointer_target);
      }
    };

    (snap.globals || []).forEach(register);
    (snap.frames || []).forEach((fr) => (fr.vars || []).forEach(register));
    return map;
  }

  /** 找出覆盖某个地址的变量 */
  function varAt(addr) {
    if (!index) return null;
    if (index.has(addr)) return index.get(addr);
    for (const [start, info] of index) {
      if (addr >= start && addr < start + info.size) return info;
    }
    return null;
  }

  /** 栈帧范围表 */
  function frameRanges() {
    return ((snapshot && snapshot.frames) || []).map((f) => ({
      low: f.low, high: f.high, index: f.index, func: f.func,
    })).filter((f) => f.low && f.high);
  }

  /** 找出某个字节属于哪一层栈帧 */
  function frameAt(addr, ranges) {
    for (const fr of ranges) {
      if (addr >= fr.low && addr < fr.high) return fr;
    }
    return null;
  }

  /** 堆块表 */
  function heapBlocks() {
    return ((snapshot && snapshot.heap) || []);
  }

  /* ---------------------------------------------------------
     渲染入口
     --------------------------------------------------------- */

  function render(snap) {
    snapshot = snap;
    const host = document.getElementById("mem-regions");
    host.innerHTML = "";

    // 连线层是独立的 SVG，不跟着 host 一起清 —— 这里必须手动清，
    // 否则程序退出 / 编译失败时，上一次的线会孤零零留在空面板上。
    const varLayer = document.getElementById("var-layer");
    const ptrLayer = document.getElementById("ptr-layer");
    if (varLayer) varLayer.innerHTML = "";
    if (ptrLayer) ptrLayer.innerHTML = "";
    clearHot();

    // 编译没过的时候根本没有运行时数据 —— 说清楚，别留一片空白。
    if (!snap) {
      index = new Map();
      pointerTargets = new Set();
      host.innerHTML = '<div class="empty-state">'
        + '<div class="empty-title">还没有运行时数据</div>'
        + '<div class="empty-sub">程序编译通过并启动后，这里会显示内存视图。</div>'
        + '</div>';
      return;
    }

    index = buildIndex(snap);

    // 未执行：能看到段布局，但还没有栈和堆的实际字节
    if (snap.state === "not-started") {
      const tip = document.createElement("div");
      tip.className = "not-started-tip";
      tip.innerHTML = "程序尚未执行 —— 这里显示的是<b>可执行文件的段布局</b>（"
        + "全局变量将住在哪里）。按「调试」开始后，这里会显示真实的内存字节。";
      host.appendChild(tip);
    }

    const regions = (snap.regions || []).slice();
    if (regions.length === 0) {
      host.innerHTML = '<div class="pointer-legend">暂无内存区域信息</div>';
      return;
    }

    const mode = document.getElementById("mem-sort").value;
    if (mode === "address") {
      regions.sort((a, b) => a.start - b.start);
    } else {
      regions.sort((a, b) => {
        const ia = KIND_ORDER.indexOf(a.kind), ib = KIND_ORDER.indexOf(b.kind);
        if (ia !== ib) return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
        return a.start - b.start;
      });
    }

    for (const region of regions) {
      host.appendChild(renderRegion(region));
    }

    renderLegend();
    requestAnimationFrame(() => {
      drawPointers();
      drawVarLinks();
    });
  }

  function renderLegend() {
    const host = document.getElementById("legend");
    const items = [
      ["var(--v-int)", "普通变量"],
      ["var(--v-array)", "数组"],
      ["var(--v-char)", "字符"],
      ["var(--v-string)", "字符串"],
      ["var(--v-ptr)", "指针"],
      ["var(--v-ptr-target)", "指向的目标"],
    ];
    host.innerHTML = items.map(([c, t]) =>
      `<span class="legend-item"><span class="legend-swatch" style="background:${c}"></span>${t}</span>`
    ).join("");
  }

  /* ---------------------------------------------------------
     单个区域
     --------------------------------------------------------- */

  function renderRegion(region) {
    const el = document.createElement("div");
    el.className = "region";
    el.dataset.kind = region.kind;

    const color = KIND_COLOR[region.kind] || "#8b95a7";
    const head = document.createElement("div");
    head.className = "region-head";
    head.innerHTML = `
      <span class="region-bar" style="background:${color}"></span>
      <span class="region-name">${escapeHtml(region.label || KIND_NAME[region.kind] || region.kind)}</span>
      <span class="region-note">${region.members && region.members.length ? escapeHtml(region.members.slice(0, 5).join("、")) : ""}</span>
      <span class="region-addr">${hex16(region.start)} – ${hex16(region.end)}</span>
    `;
    head.addEventListener("click", () => {
      el.classList.toggle("collapsed");
      // 收起/展开会改变所有行的位置，连线必须重画
      requestAnimationFrame(redrawAll);
    });
    el.appendChild(head);

    const body = document.createElement("div");
    body.className = "region-body";

    const chunks = (snapshot.chunks || []).filter((c) => overlaps(c, region));
    if (chunks.length === 0) {
      // 说清楚「为什么没有字节」，而不是笼统一句「没有内容」。
      // .data / .bss 是重点：这两个段为什么空、变量为什么会进另一段，
      // 初学者最容易在这里以为采集出错了。
      body.innerHTML = `<div class="pointer-legend">${escapeHtml(emptyRegionHint(region))}</div>`;
    } else {
      for (const chunk of chunks) {
        body.appendChild(renderChunk(chunk));
      }
    }

    el.appendChild(body);
    return el;
  }

  /** 分区里没有字节时的一句话解释（.data / .bss 要说明分段规则）。 */
  function emptyRegionHint(region) {
    // 「有没有全局/静态变量」要看整张快照：某个段空着，不代表程序里没有
    // 这类变量 —— 常见情况是变量全在另一段。
    const anyStatic = ((snapshot && snapshot.globals) || []).length > 0;

    if (region.kind === "data") {
      if (!anyStatic) {
        return "这个程序里还没有全局或静态变量，所以 .data 是空的。";
      }
      return "这段是空的：只有「初值不是全 0」的全局与静态变量才进 .data"
        + "（非 0 初值要原样存进可执行文件）。你这些变量的初值都是 0，所以都住在 .bss。";
    }
    if (region.kind === "bss") {
      if (!anyStatic) {
        return "这个程序里还没有全局或静态变量，所以 .bss 也是空的。";
      }
      return "这段是空的：没有「初值全 0 / 没写初值」的全局或静态变量 —— "
        + "它们的初值都不是 0，所以全在 .data。";
    }

    const why = {
      text: "程序段：可执行文件里没有对应的机器指令可读（还没开始执行，或当前函数不在镜像里）",
      rdata: "只读数据（字符串常量）的字节会在它被引用时单独展开",
      heap: "堆上还没有 malloc 出来的数据",
      stack: "栈区域在程序运行后才有内容",
    };
    return why[region.kind] || "这个区域没有采集到具体字节内容";
  }

  function overlaps(chunk, region) {
    const cs = chunk.start;
    const ce = chunk.start + chunk.size;
    return cs < region.end && ce > region.start;
  }

  /* ---------------------------------------------------------
     HEX 网格
     --------------------------------------------------------- */

  function renderChunk(chunk) {
    const wrap = document.createElement("div");
    wrap.className = "hex-wrap";

    const limit = RENDER_LIMIT[chunk.kind] || 2048;
    const total = chunk.size;
    const show = Math.min(total, limit);

    const data = chunk.data;
    const startAddr = chunk.start;
    const isStack = chunk.kind === "stack";
    const ranges = isStack ? frameRanges() : [];
    const heaps = chunk.kind === "heap" ? heapBlocks() : [];

    // 分成 16 字节一行
    for (let off = 0; off < show; off += 16) {
      const rowAddr = startAddr + off;
      const row = document.createElement("div");
      row.className = "hexrow";

      // 栈帧色带：让「每层递归各自一段」一眼可见
      if (isStack && ranges.length) {
        const fr = frameAt(rowAddr, ranges);
        const stripe = document.createElement("span");
        stripe.className = "frame-stripe";
        if (fr) {
          stripe.style.background = frameColor(fr.index, 0.95);
          stripe.title = "栈帧 #" + fr.index + "  " + fr.func + "()";
        }
        row.appendChild(stripe);
      }

      const addrCell = document.createElement("div");
      addrCell.className = "hex-addr";
      addrCell.dataset.addr = String(rowAddr);
      addrCell.textContent = hex16(rowAddr);
      // 指到地址上，也把这一行里起始的变量一起点亮
      addrCell.addEventListener("mouseenter", () => setHotRow(rowAddr));
      addrCell.addEventListener("mouseleave", clearHot);
      row.appendChild(addrCell);

      const bytesCell = document.createElement("div");
      bytesCell.className = "hex-bytes";

      let ascii = "";
      // 本行起始的变量/堆块 —— 先收集，等这一行的字节都排好后再统一挂到右侧
      const chips = [];

      for (let i = 0; i < 16; i++) {
        const idx = off + i;
        if (idx >= show) {
          const sp = document.createElement("span");
          sp.className = "hex-byte";
          sp.textContent = "  ";
          bytesCell.appendChild(sp);
          continue;
        }
        const addr = rowAddr + i;
        const byteHex = data.substr(idx * 2, 2);
        const b = parseInt(byteHex, 16);

        const cell = document.createElement("span");
        cell.className = "hex-byte";
        cell.textContent = byteHex;
        cell.dataset.addr = String(addr);
        if (b === 0) cell.classList.add("zero");

        const owner = varAt(addr);
        if (owner) {
          const color = TYPE_COLOR[owner.kind] || TYPE_COLOR.unknown;
          cell.style.background = color;
          cell.style.opacity = owner.kind === "ptr" ? "0.85" : "0.5";
          cell.classList.add("highlight");
          cell.style.color = "var(--bg)";
          // 首尾两格带圆角，中间直角 —— 同色贴在一起就是一条长方形
          if (addr === owner.address) cell.classList.add("var-open");
          if (addr === owner.address + owner.size - 1) cell.classList.add("var-close");
        }
        if (pointerTargets.has(addr)) {
          cell.style.background = "var(--v-ptr-target)";
          cell.style.opacity = "1";
          cell.style.color = "#0b1220";
          cell.style.fontWeight = "700";
        }

        if (owner && owner.address === addr) {
          chips.push({ kind: "var", addr, info: owner });
        }

        // 堆块起始处挂标签
        for (const hb of heaps) {
          if (hb.address === addr) {
            chips.push({ kind: "heap", addr, heap: hb });
            break;
          }
        }

        // 栈帧起点也排到右边去 —— 贴在字节上会挡掉一格字节
        for (const fr of ranges) {
          if (fr.low === addr) {
            // 放到最前面：它标的是「这一层从哪儿开始」，排在变量前面读起来顺
            chips.unshift({ kind: "frame", addr, frame: fr });
            break;
          }
        }

        cell.addEventListener("mouseenter", (e) => {
          showTooltip(e, addr, owner);
          setHot(owner ? owner.address : null);
        });
        cell.addEventListener("mousemove", moveTooltip);
        cell.addEventListener("mouseleave", () => {
          hideTooltip();
          clearHot();
        });

        bytesCell.appendChild(cell);
        ascii += (b >= 32 && b < 127) ? String.fromCharCode(b) : ".";
      }

      row.appendChild(bytesCell);

      if (document.getElementById("toggle-ascii").checked) {
        const asciiCell = document.createElement("div");
        asciiCell.className = "hex-ascii";
        asciiCell.textContent = ascii;
        row.appendChild(asciiCell);
      }

      // 变量标签统一排到行的最右边，和字节之间用一根细线连起来
      if (chips.length) {
        row.appendChild(buildChipBar(chips));
      }

      wrap.appendChild(row);
    }

    if (total > show) {
      const note = document.createElement("div");
      note.className = "pointer-legend";
      note.textContent = `… 该区域共 ${total} 字节，此处只显示前 ${show} 字节`;
      wrap.appendChild(note);
    }

    if (chunk.label) {
      const label = document.createElement("div");
      label.className = "pointer-legend";
      label.style.color = "var(--fg-faint)";
      label.textContent = chunk.label;
      wrap.appendChild(label);
    }

    return wrap;
  }

  /* ---------------------------------------------------------
     行右侧的变量标签
     --------------------------------------------------------- */

  function buildChipBar(chips) {
    const bar = document.createElement("div");
    bar.className = "row-vars";

    for (const c of chips) {
      const isHeap = c.kind === "heap";
      const isFrame = c.kind === "frame";
      const info = c.info;
      const color = isFrame
        ? frameColor(c.frame.index, 1)
        : isHeap
          ? "hsl(280 60% 62%)"
          : (TYPE_COLOR[info.kind] || TYPE_COLOR.unknown);

      const chip = document.createElement("span");
      chip.className = "var-chip"
        + (isHeap ? " heap-chip" : "")
        + (isFrame ? " frame-chip" : "");
      chip.style.setProperty("--chip-color", color);

      if (isFrame) {
        // 栈帧标签不连线：它标的是「这一层从哪儿开始」，不是某个变量
        chip.textContent = "栈帧 #" + c.frame.index + " " + c.frame.func + "()";
        chip.title = "栈帧 #" + c.frame.index + " " + c.frame.func
          + "() 从 " + hex16(c.addr) + " 开始";
        bar.appendChild(chip);
        continue;
      }

      chip.dataset.varKey = String(c.addr);
      chip.dataset.color = color;

      if (isHeap) {
        chip.textContent = "堆块 " + c.heap.size + " 字节";
        chip.title = "堆块起始于 " + hex16(c.addr) + "，共 " + c.heap.size + " 字节";
      } else {
        // 只写变量名：类型已经由底色表达（和顶部图例一致），
        // 一行常常并排好几个变量，带上类型会把整行挤爆
        chip.textContent = info.name;
        chip.title = info.name + " (" + (info.type || "?") + ") 起始于 "
          + hex16(c.addr) + "，占 " + info.size + " 字节";
        // 指针的话顺手把「指向哪儿」写进悬停提示 —— 视图外的箭头改成了
        // 徽标，地址就只剩这里能看到了
        if (info.isPointer && info.pointerTarget != null) {
          chip.title += "，指向 " + hex16(info.pointerTarget);
        }
      }

      // 指到标签＝点亮它占的字节、地址和那根线；指到字节也一样反着来
      chip.addEventListener("mouseenter", () => setHot(c.addr));
      chip.addEventListener("mouseleave", clearHot);

      bar.appendChild(chip);
    }
    return bar;
  }

  /* ---------------------------------------------------------
     联动高亮：变量标签 ⇄ 字节 ⇄ 地址 ⇄ 指向线
     --------------------------------------------------------- */

  function clearHot() {
    document.querySelectorAll(".hot").forEach((el) => el.classList.remove("hot"));
  }

  /** 点亮一个变量：它的标签、它占的每个字节、所在行的地址、指向线 */
  function setHot(addr) {
    clearHot();
    if (addr == null) return;

    const info = index.get(addr) || varAt(addr);
    const start = info ? info.address : addr;
    const end = start + (info && info.size ? info.size : 1);

    // 标签和指向线都带 data-var-key，一起点亮
    document.querySelectorAll(`[data-var-key="${start}"]`).forEach((el) => {
      el.classList.add("hot");
    });

    for (let a = start; a < end; a++) {
      const cell = document.querySelector(`.hex-byte[data-addr="${a}"]`);
      if (cell) cell.classList.add("hot");
    }

    const anchor = document.querySelector(`.hex-byte[data-addr="${start}"]`);
    if (anchor) {
      const row = anchor.closest(".hexrow");
      const addrCell = row && row.querySelector(".hex-addr");
      if (addrCell) addrCell.classList.add("hot");
    }
  }

  /** 指到地址列：把这一行（16 字节）里起始的变量全点亮 */
  function setHotRow(rowAddr) {
    clearHot();
    const keys = [];
    for (const chip of document.querySelectorAll(".row-vars .var-chip")) {
      const a = Number(chip.dataset.varKey);
      if (a >= rowAddr && a < rowAddr + 16) keys.push(a);
    }
    if (!keys.length) {
      // 这一行没有变量起始，也让地址本身亮一下，表示「这里可点」
      const cell = document.querySelector(`.hex-addr[data-addr="${rowAddr}"]`);
      if (cell) cell.classList.add("hot");
      return;
    }
    keys.forEach((k) => setHot(k));
  }

  /* ---------------------------------------------------------
     变量标签 → 起始字节 的指向线
     --------------------------------------------------------- */

  function drawVarLinks() {
    const layer = document.getElementById("var-layer");
    if (!layer) return;
    layer.innerHTML = "";

    // 按行分组：一行里可能有好几个变量，它们的线要在行距里错开，否则叠成一条
    document.querySelectorAll(".row-vars").forEach((bar) => {
      const row = bar.closest(".hexrow");
      if (!row) return;
      const rowBox = row.getBoundingClientRect();
      const chips = Array.from(bar.children);
      // 只数「真的要画线」的标签：栈帧标签、视图外徽标也住在这个容器里，
      // 拿它们在 children 里的下标当层号，会让线的高度随无关元素变化而跳动
      let laneIdx = 0;

      chips.forEach((chip) => {
        if (!chip.dataset.varKey) return;   // 栈帧标签 / 视图外徽标不连线
        const addr = Number(chip.dataset.varKey);
        const target = document.querySelector(`.hex-byte[data-addr="${addr}"]`);
        if (!target) return;   // 目标不在渲染范围内，就别画一根断线

        const s = chip.getBoundingClientRect();
        const t = target.getBoundingClientRect();

        const x1 = s.left + 8;
        const y1 = s.bottom - 1;
        const x2 = t.left + t.width / 2;
        const y2 = t.bottom - 1;
        // 通道落在行的下半段空档里：从行底往上数，同一行的线依次错开。
        // 至少要离行内容（20px 处）4px 远，否则贴着色带下沿走，还是像压在字上。
        const lane = rowBox.bottom - 4 - (laneIdx % 3) * 3;
        laneIdx += 1;

        // 直角走线：从标签下来 → 沿行距横着走 → 再上去点到字节
        const d =
          `M ${x1} ${y1} L ${x1} ${lane} L ${x2} ${lane} L ${x2} ${y2}`;

        const color = chip.dataset.color || "var(--fg-faint)";
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", d);
        path.dataset.varKey = String(addr);
        path.style.setProperty("--link-color", color);
        layer.appendChild(path);

        // 目标端一个小圆点，指明「就是这个字节」
        const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        dot.setAttribute("cx", x2);
        dot.setAttribute("cy", y2);
        dot.setAttribute("r", "2.2");
        dot.dataset.varKey = String(addr);
        dot.style.setProperty("--link-color", color);
        layer.appendChild(dot);
      });
    });
  }

  /* ---------------------------------------------------------
     指针连线
     --------------------------------------------------------- */

  function drawPointers() {
    const layer = document.getElementById("ptr-layer");
    if (!layer) return;
    layer.innerHTML = "";

    // 上一轮加的「视图外」徽标要先摘掉，否则每重画一次就多一个
    document.querySelectorAll(".ptr-hint").forEach((n) => n.remove());

    for (const [addr, info] of index) {
      if (!info.isPointer || info.pointerTarget == null) continue;

      const srcEl = document.querySelector(`.hex-byte[data-addr="${info.address}"]`);
      const dstEl = document.querySelector(`.hex-byte[data-addr="${info.pointerTarget}"]`);
      if (!srcEl) continue;

      if (!dstEl) {
        // 目标地址没有被渲染成字节（落在当前分区的显示范围之外）。
        //
        // 这里**千万不能**画一根从字节拉到面板边缘的箭头：目标不存在时
        // 起点和终点的 y 相等，曲线退化成一条水平直线，正好从行内文字的
        // 中线上横穿过去，看起来就像一根莫名的删除线压在 ASCII 列上。
        // 改成在该行标签区挂一枚小徽标，只说明「指向哪儿」。
        addPtrHint(srcEl, info.address, info.pointerTarget);
        continue;
      }

      // 跨分区、或者隔了太多行的指针，也不画线：那种线要竖着穿过若干个
      // 分区标题和其它行，看着更像渲染错乱，不如直接标「→ 变量名」。
      const sRegion = srcEl.closest(".region");
      const dRegion = dstEl.closest(".region");
      const sRowEl = srcEl.closest(".hexrow");
      const dRowEl = dstEl.closest(".hexrow");
      if (!sRowEl || !dRowEl) continue;

      const rowGap = Math.abs(
        sRowEl.getBoundingClientRect().top - dRowEl.getBoundingClientRect().top
      );
      if (sRegion !== dRegion || rowGap > MAX_ARROW_ROWS * 34) {
        addPtrHint(srcEl, info.address, info.pointerTarget);
        continue;
      }

      drawArrow(layer, srcEl, dstEl, true);
    }
  }

  /** 指针目标不在视图内时，在该行标签区挂一枚徽标（代替画到屏幕边缘的线）。 */
  function addPtrHint(srcEl, addr, target) {
    const row = srcEl.closest(".hexrow");
    if (!row) return;
    const bar = row.querySelector(".row-vars");
    if (!bar) return;   // 没有标签区就不画了，绝不为了这个动行布局

    // 紧挨着它自己的变量标签放，否则一行两个指针时两个徽标会挤在最左边，
    // 看不出哪个是哪个。
    const chip = bar.querySelector(`.var-chip[data-var-key="${addr}"]`);
    if (!chip) return;

    const hint = document.createElement("span");
    hint.className = "ptr-hint";

    // 目标变量还在视图里（只是跨了分区 / 隔得太远）就直接报它的名字 ——
    // 比一堆十六进制地址直观得多。彻底没渲染出来才写「视图外」。
    const owner = varAt(target);
    if (owner) {
      hint.textContent = "→ " + owner.name;
      hint.title = "指向 " + owner.name + "（" + hex16(target) + "），"
        + "距离太远或跨了分区，没有画连线";
    } else {
      // 文案固定成「视图外」四个字：完整地址有 16 位十六进制，写成徽标会把
      // 标签区挤爆（一行标签总宽是有上限的），地址放进 title 里更省地方。
      hint.textContent = "→ 视图外";
      hint.title = "指向 " + hex16(target) + "，该地址不在当前显示范围内"
        + "（滚动下面的内存区或展开对应分区可以看到它）";
    }
    bar.insertBefore(hint, chip);
  }

  /* 指针箭头：从指针自己的字节，走到它指向的字节。

     走线规则和变量标签的连线一致 —— 一律下到「行内容下方的那条空档」里
     横着走，再竖着上去点住目标字节。之前用的是三次贝塞尔直接连两个字节的
     中心，只要两端在同一行（全局变量、同层局部变量最常见），起点终点的
     y 就完全相等，曲线退化成一条横穿整行文字的直线，看着像删除线。 */
  function drawArrow(layer, srcEl, dstEl, toTarget) {
    const sRow = srcEl.closest(".hexrow");
    const dRow = dstEl.closest(".hexrow");
    if (!sRow || !dRow) return;

    const s = srcEl.getBoundingClientRect();
    const d = dstEl.getBoundingClientRect();
    const sx = s.left + s.width / 2;
    const tx = d.left + d.width / 2;

    const sBox = sRow.getBoundingClientRect();
    const dBox = dRow.getBoundingClientRect();
    const sLane = sBox.bottom - 4;      // 起点那行的空档
    const dLane = dBox.bottom - 4;      // 目标那行的空档

    // 两个字节的底边：线贴在这里进出，不压到数字本身
    const sy = s.bottom - 1;
    const ty = d.bottom - 1;

    let dAttr;
    if (Math.abs(sBox.top - dBox.top) < 1) {
      // 同一行：起点 → 下到空档 → 横着走 → 竖着上来
      dAttr = `M ${sx} ${sy} L ${sx} ${sLane} L ${tx} ${sLane} L ${tx} ${ty}`;
    } else {
      // 跨行：先在本行空档里横到目标那一列，再沿这一列上下，最后点进去
      dAttr = `M ${sx} ${sy} L ${sx} ${sLane} L ${tx} ${sLane} ` +
              `L ${tx} ${dLane} L ${tx} ${ty}`;
    }

    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", dAttr);
    if (toTarget) path.classList.add("to-target");
    layer.appendChild(path);

    // 箭头改成「两笔小尖」而不是一个圆点：方向一眼看得出来
    const head = document.createElementNS("http://www.w3.org/2000/svg", "path");
    head.setAttribute("d", `M ${tx - 3} ${ty + 5} L ${tx} ${ty} L ${tx + 3} ${ty + 5}`);
    if (toTarget) head.classList.add("to-target");
    layer.appendChild(head);
  }

  /* ---------------------------------------------------------
     Tooltip
     --------------------------------------------------------- */

  function showTooltip(e, addr, owner) {
    const tip = document.getElementById("tooltip");
    let html = `<div class="tt-name">${escapeHtml(owner ? owner.name : "地址")}</div>`;
    html += `<div class="tt-row">${hex16(addr)}</div>`;
    if (owner) {
      html += `<div class="tt-row">类型 ${escapeHtml(owner.type || "?")} · ${owner.size} 字节</div>`;
      if (owner.value) html += `<div class="tt-row">值 ${escapeHtml(owner.value)}</div>`;
      if (owner.isPointer && owner.pointerTarget != null) {
        html += `<div class="tt-row">→ 指向 ${hex16(owner.pointerTarget)}</div>`;
      }
      if (owner.preview) html += `<div class="tt-row">内容 "${escapeHtml(owner.preview)}"</div>`;
      if (owner.issues && owner.issues.length) {
        html += `<div class="tt-note">⚠ ${escapeHtml(owner.issues.join("；"))}</div>`;
      }
    }
    tip.innerHTML = html;
    tip.hidden = false;
    moveTooltip(e);
  }

  function moveTooltip(e) {
    const tip = document.getElementById("tooltip");
    if (tip.hidden) return;
    const pad = 14;
    let x = e.clientX + pad;
    let y = e.clientY + pad;
    const r = tip.getBoundingClientRect();
    if (x + r.width > window.innerWidth - 8) x = e.clientX - r.width - pad;
    if (y + r.height > window.innerHeight - 8) y = e.clientY - r.height - pad;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }

  function hideTooltip() {
    document.getElementById("tooltip").hidden = true;
  }

  /* ---------------------------------------------------------
     聚焦某个地址
     --------------------------------------------------------- */

  function focusAddress(addr) {
    if (addr == null) return;
    const el = document.querySelector(`.hex-byte[data-addr="${addr}"]`);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.style.transition = "none";
    el.style.boxShadow = "0 0 0 2px var(--accent)";
    setTimeout(() => { el.style.boxShadow = ""; }, 1400);
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function redrawAll() {
    drawPointers();
    drawVarLinks();
  }

  return {
    render, focusAddress, hex16, hex8, escapeHtml,
    // 滚动 / 分栏拖动后两层线都要跟着重画
    redrawPointers: redrawAll,
  };
})();
