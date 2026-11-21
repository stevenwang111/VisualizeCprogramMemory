/* ============================================================
   addr.js —— 地址显示格式

   完整地址是 0x00007FF701F61000 这种 16 位十六进制，又长又占地方。
   这个模块负责统一决定地址怎么显示：

     完整   —— 始终 0x + 16 位十六进制
     紧凑   —— 始终 0x + 去掉前导零
     自动   —— 高 32 位为 0 就折叠成紧凑，否则展开成完整

   「自动」是默认：栈、堆这类低地址空间用紧凑视图，
   而代码段、共享库那些高地址自动展开，两边都能看清。
   用户可以在界面上随时改成另外两种。
   ============================================================ */

const Addr = (() => {

  const MODES = { AUTO: "auto", FULL: "full", COMPACT: "compact" };
  const KEY = "cdbg-addr-mode";
  let mode = MODES.AUTO;

  function load() {
    try {
      const saved = localStorage.getItem(KEY);
      if (saved === MODES.FULL || saved === MODES.COMPACT || saved === MODES.AUTO) {
        mode = saved;
      }
    } catch (e) { /* ignore */ }
    return mode;
  }

  function save() {
    try { localStorage.setItem(KEY, mode); } catch (e) { /* ignore */ }
  }

  function set(m) {
    mode = (m === MODES.FULL || m === MODES.COMPACT) ? m : MODES.AUTO;
    save();
    return mode;
  }

  function get() { return mode; }

  function toHex(value) {
    // 地址可能超过 32 位，必须走 BigInt，否则 >>> 0 会截断
    try {
      const big = BigInt(Math.trunc(value));
      if (big < 0n) return (-big).toString(16).toUpperCase();
      return big.toString(16).toUpperCase();
    } catch (e) {
      return "0";
    }
  }

  /** 自动模式下：高 32 位为 0 就折叠 */
  function isLow(value) {
    const v = Math.trunc(value);
    return v >= 0 && v <= 0xFFFFFFFF;
  }

  function format(value) {
    if (value === null || value === undefined || value === "") return "—";
    const v = Number(value);
    if (!isFinite(v)) return "—";

    switch (mode) {
      case MODES.FULL:
        return "0x" + toHex(v).padStart(16, "0");
      case MODES.COMPACT:
        return "0x" + toHex(v);
      default:
        return isLow(v)
          ? "0x" + toHex(v)
          : "0x" + toHex(v).padStart(16, "0");
    }
  }

  /** 宽度提示：用于让地址列不至于忽宽忽窄 */
  function formatRange(start, end) {
    return format(start) + " – " + format(end);
  }

  return { MODES, format, formatRange, set, get, load, save };
})();
