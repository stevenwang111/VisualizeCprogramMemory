"""嵌入 GDB 执行的采集脚本。

这个文件 **不是** 由主进程 import 的，而是通过 `gdb -x gdb_probe.py` 加载，
在 GDB 内部运行。

## 为什么用事件回调而不是命令调用

MI 的 `-data-evaluate-expression` 在当前语言上下文（这里是 C）里求值，
找不到 Python 函数（实测报 `No symbol "cdbg_collect" in current context`）。
而 `gdb.events.stop` 回调是 GDB 内部同步触发的：每次程序停下来，
采集就自动完成一次，采集结果原子写入约定文件。主进程只需要读文件。

## 两条铁律

1. **绝不要 print 中文到 stdout** —— GDB 的 stdout 走 GBK 编码，
   遇到中文会抛 UnicodeDecodeError 直接崩掉。日志一律用英文。
2. **写文件一律用 `io.open(..., encoding="utf-8")`**，并且用
   `os.replace` 原子替换，避免主进程读到写了一半的 JSON。
"""

import io
import json
import os
import re

import gdb

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

OUT_PATH = os.environ.get("CDBG_OUT", "")
if not OUT_PATH:
    OUT_PATH = os.path.join(os.environ.get("TEMP", "."), "cdbg_snapshot.json")

#: 日志文件。刻意 **不** 写 stdout / stderr ——
#: MI 模式下 stdout 是协议流，任何多余的字节都会让 GDB 的解析器错乱，
#: 严重时直接让 GDB 退出。日志只进文件。
LOG_PATH = os.environ.get("CDBG_LOG", "")

#: 段布局缓存 —— 运行期不会变，缓存能省掉每次都解析 info files 的开销
_SECTION_CACHE = None

#: 静态存储变量清单的缓存。键是 (pid, 是否运行中)：
#: 程序重启后 ASLR 会换地址，缓存必须跟着失效。
_STATIC_CACHE = {"key": None, "items": []}


def _program_running():
    """被调试程序是否正在运行。

    这个判断很关键：程序没跑时 GDB 给的是**文件虚拟地址**，
    程序跑起来后 ASLR 会重定位，地址就变了。段布局必须跟着变。
    """
    try:
        return int(gdb.selected_inferior().pid) > 0
    except Exception:
        return False


#: 每次采集递增的序号，主进程靠它判断"文件是不是更新了"
_SEQ = {"n": 0}

#: 段名 -> 区域种类。
#:
#: Windows（PE）和 Linux（ELF）的段名不同：PE 用 .rdata，ELF 用
#: .rodata / .data.rel.ro。两边都在这里统一成教学用的四种 kind。
_SECTION_KIND = {
    ".text": "text",
    ".data": "data",
    ".rdata": "rdata",
    ".rodata": "rdata",
    ".data.rel.ro": "rdata",
    ".bss": "bss",
    ".tbss": "bss",
    ".tdata": "data",
}

#: 段白名单：只保留对教学有意义的段。
#:
#: 用白名单而不是「忽略表」：ELF 的段名远比 PE 多
#: （.interp/.dynsym/.rela.*/.plt/.eh_frame/...），补全忽略表既写不完，
#: 也容易随工具链版本变化而漏。白名单天然把无关段挡在外面。
_TEACHING_SECTIONS = set(_SECTION_KIND)

#: `mt print symbols` 里的一行静态存储符号，形如：
#:
#:     int c; static at 0x7ff7f2387030 section .bss
#:     int a[3]; static at 0x140007030 section .data
#:
#: 这是唯一能把「函数里的 static」连同地址、所在段一起拿到的输出。
_STATIC_SYM_RE = re.compile(
    r"^\s+(?P<type>.+?)\s+(?P<name>[A-Za-z_]\w*)(?P<dims>(?:\[[^\]]*\])*)"
    r"\s*;\s*static at\s+(?P<addr>0x[0-9a-fA-F]+)"
    r"(?:\s+section\s+(?P<section>\S+))?"
)

#: block 头里的函数名：`block #002, ..., function main`
_BLOCK_FUNC_RE = re.compile(r",\s*function\s+([A-Za-z_]\w*)\s*$")


def _log(msg):
    """写日志到文件。

    绝对不要写 stdout 或 stderr：MI 模式下 stdout 是 GDB 的协议通道，
    混入任何额外字节都会破坏解析。日志只进文件。
    """
    if not LOG_PATH:
        return
    try:
        with io.open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("[cdbg] %s\n" % msg)
    except Exception:
        pass


def _write_json(obj):
    """原子写出快照 JSON。"""
    global _SEQ
    _SEQ["n"] += 1
    obj["seq"] = _SEQ["n"]
    tmp = OUT_PATH + ".tmp"
    try:
        with io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False))
        os.replace(tmp, OUT_PATH)
    except Exception as exc:
        _log("write failed: %s" % exc)


# ---------------------------------------------------------------------------
# 段布局
# ---------------------------------------------------------------------------


def _parse_sections():
    """解析 `info files`，返回主可执行文件的段列表。

    输出格式（实测）：
        0x00007ff66cf31000 - 0x00007ff66cf32800 is .text
        0x00007ffacaec1000 - 0x00007ffacb02bd5c is .text in C:\\...\\ntdll.dll

    带 " in <路径>" 后缀的是系统 DLL 的段，我们要的是主 exe 的段。
    """
    global _SECTION_CACHE
    # 缓存只在程序运行期间有效。
    #
    # 程序没跑的时候，`info files` 给的是**文件虚拟地址**（比如 0x140003000）；
    # 程序跑起来后，Windows 的 ASLR 会把 exe 重定位到 0x7FF7... 这种高位地址，
    # 这时 `info files` 给的才是运行时的真实地址。
    #
    # 之前把未运行时的结果缓存了下来，导致运行后全局变量明明在 .data 段里，
    # 却因为地址对不上而显示不出字节内容。
    if _SECTION_CACHE is not None and _program_running():
        return _SECTION_CACHE

    sections = []
    try:
        info = gdb.execute("info files", to_string=True)
    except Exception as exc:
        _log("info files failed: %s" % exc)
        if _program_running():
            _SECTION_CACHE = []
        return _SECTION_CACHE or []

    for line in info.split("\n"):
        line = line.strip()
        if " is " not in line:
            continue
        # 排除带 " in <dll路径>" 的行
        head, _, tail = line.partition(" is ")
        if " in " in tail:
            continue
        name = tail.strip()
        if not head.startswith("0x"):
            continue
        lo_s, _, hi_s = head.partition(" - ")
        try:
            lo = int(lo_s.strip(), 16)
            hi = int(hi_s.strip(), 16)
        except ValueError:
            continue
        if name not in _TEACHING_SECTIONS:
            continue
        sections.append(
            {
                "name": name,
                "start": lo,
                "end": hi,
                "kind": _SECTION_KIND.get(name, "data"),
            }
        )

    sections = _merge_sections(sections)

    # 程序没跑时不缓存 —— 那份是文件虚拟地址，下次运行后就是错的
    if _program_running():
        _SECTION_CACHE = sections
    else:
        _SECTION_CACHE = None
    return sections


def _merge_sections(sections):
    """合并相邻 / 重叠的同类型段。

    ELF 的代码段常被拆成 .init/.plt/.text/.fini 等几块；白名单虽然只留下
    .text，但 .data 与 .tdata、.data.rel.ro 之间也可能出现同 kind 的邻接
    区间。合并后内存视图里不会出现两个「数据段」分区。
    """
    merged = []
    for sec in sorted(sections, key=lambda s: s["start"]):
        if merged:
            prev = merged[-1]
            if prev["kind"] == sec["kind"] and sec["start"] <= prev["end"]:
                prev["end"] = max(prev["end"], sec["end"])
                if sec["name"] not in prev["name"].split(" + "):
                    prev["name"] += " + " + sec["name"]
                continue
        merged.append(dict(sec))
    return merged


def _locate(addr):
    """判断一个地址落在哪个段，返回 (段名, 区域种类) 或 (None, None)。"""
    if addr is None:
        return None, None
    for sec in _parse_sections():
        if sec["start"] <= addr < sec["end"]:
            return sec["name"], sec["kind"]
    return None, None


# ---------------------------------------------------------------------------
# 内存读取
# ---------------------------------------------------------------------------


def _read_bytes(addr, size):
    """读一段内存，返回 (hex字符串, 实际读到的字节数)。失败返回 ("", 0)。"""
    if not addr or size <= 0:
        return "", 0
    size = min(int(size), 4096)
    try:
        mem = gdb.selected_inferior().read_memory(int(addr), size)
        return mem.tobytes().hex(), size
    except Exception:
        # 逐字节回退：可能跨越了未提交的页
        out = bytearray()
        for i in range(size):
            try:
                b = gdb.selected_inferior().read_memory(int(addr) + i, 1).tobytes()
                out += b
            except Exception:
                break
        return out.hex(), len(out)


def _read_cstring(addr, limit=64):
    """读取 C 字符串（用于判断指针是否指向字符串常量）。"""
    if not addr:
        return None
    try:
        mem = gdb.selected_inferior().read_memory(int(addr), limit)
        raw = mem.tobytes()
    except Exception:
        return None
    end = raw.find(b"\x00")
    if end < 0:
        return None
    text = raw[:end]
    if not text:
        return None
    try:
        decoded = text.decode("utf-8")
    except UnicodeDecodeError:
        try:
            decoded = text.decode("gbk")
        except UnicodeDecodeError:
            return None
    # 必须全是可打印字符，否则不是字符串
    for ch in decoded:
        if not (ch.isprintable() or ch in "\t\n\r"):
            return None
    return decoded


# ---------------------------------------------------------------------------
# 变量采集
# ---------------------------------------------------------------------------


def _array_length(vtype):
    """若是数组，返回元素个数。

    注意 `gdb.Type.range()` 给的是 **(下界, 上界) 二元组**，不是三元组。
    以前按三个值解包，每次都抛 ValueError 被 except 吞掉，于是
    `array_length` 永远是 None —— 数组在界面上从来没被当成数组过
    （没长度、没数组配色、第 5 章的教学提示也一直不出现）。
    这里两种形状都容错。
    """
    try:
        base = vtype.strip_typedefs()
        if base.code != gdb.TYPE_CODE_ARRAY:
            return None
        rng = base.range()
        if len(rng) >= 3:
            return max(0, int(rng[2]))
        lo, hi = rng[0], rng[1]
        return max(0, int(hi) - int(lo) + 1)
    except Exception:
        return None


def _is_pointer(vtype):
    try:
        return vtype.strip_typedefs().code == gdb.TYPE_CODE_PTR
    except Exception:
        return False


def _is_char_pointer(vtype):
    """判断是不是「字符指针」。

    只有 char* 才按 C 字符串去读内容。
    否则 int** 会被当成字符串读出一堆无意义的可打印字符
    （实测 int **pp 会显示成 "组_" 这种东西）。
    """
    try:
        base = vtype.strip_typedefs()
        if base.code != gdb.TYPE_CODE_PTR:
            return False
        target = base.target()
        name = (target.name or "").strip()
        return name in ("char", "signed char", "unsigned char")
    except Exception:
        return False


def _describe_value(v, is_pointer=False):
    """把变量值转成适合初学者看的字符串。

    指针必须特殊处理：直接 str(v) 会让 GDB 顺着指针链一路解引用，
    最后按 C 字符串规则打印出来 —— 例如 int **pp 会显示成 "组_" 这种
    完全误导的内容。指针一律显示它存的地址。
    """
    if is_pointer:
        try:
            return "0x%016X" % int(v)
        except Exception:
            try:
                return "0x%016X" % int(v.address)
            except Exception:
                return "0x0000000000000000"
    try:
        return str(v)
    except Exception:
        return "<unreadable>"


def _collect_var(frame, sym, scope, frame_index):
    """采集单个变量。"""
    info = {
        "name": sym.name,
        "type": "",
        "size": 0,
        "address": None,
        "value": "",
        "scope": scope,
        "frame": frame_index,
        "is_pointer": False,
        "pointer_target": None,
        "pointer_target_name": None,
        "storage": "unknown",
        "array_length": None,
        "is_string": False,
        "preview": None,
        "issues": [],
        "decl_line": None,
        "decl_func": None,
    }
    try:
        v = frame.read_var(sym)
    except Exception as exc:
        info["issues"].append("无法读取: %s" % str(exc)[:60])
        return info

    try:
        info["decl_line"] = int(sym.line)
    except Exception:
        info["decl_line"] = None

    try:
        vtype = v.type
        info["type"] = str(vtype)
        info["size"] = int(vtype.sizeof)
    except Exception:
        pass

    try:
        addr = v.address
        info["address"] = int(addr) if addr is not None else None
    except Exception:
        info["address"] = None

    info["value"] = _describe_value(v, _is_pointer(v.type))

    # 指针：解析指向的目标
    if _is_pointer(v.type):
        info["is_pointer"] = True
        try:
            target = int(v)
            info["pointer_target"] = target
        except Exception:
            target = None
        if target:
            if _is_char_pointer(v.type):
                text = _read_cstring(target)
                if text is not None:
                    info["is_string"] = True
                    info["preview"] = text
            sec, kind = _locate(target)
            if sec:
                info["pointer_target_name"] = sec

    # 数组
    length = _array_length(v.type)
    if length is not None:
        info["array_length"] = length

    # 存储位置归类
    #
    # 不能按「作用域」直接猜：函数里的 `static int c;` 作用域是函数，但存在
    # 静态存储区（.data / .bss），根本不在栈上。之前一律按 scope 判成 stack，
    # 导致内存视图去栈里找它 —— 当然找不到，字节也就没采集。
    addr = info["address"]
    if addr:
        _, kind = _locate(addr)
        if kind in ("data", "bss", "rdata", "text"):
            info["storage"] = kind
        else:
            # 不在任何已知段里：普通局部变量就落在栈帧上
            info["storage"] = "stack" if scope not in ("global", "static") else "unknown"
    else:
        info["storage"] = "unknown"

    return info


def _collect_frame(frame, index):
    """采集一个栈帧及其局部变量。"""
    info = {
        "index": index,
        "func": frame.name() or "?",
        "file": "",
        "line": 0,
        "pc": 0,
        "rsp": 0,
        "rbp": 0,
        "low": 0,
        "high": 0,
        "size": 0,
        "vars": [],
    }
    try:
        sal = frame.find_sal()
        if sal is not None:
            if sal.symtab is not None:
                info["file"] = sal.symtab.fullname()
            info["line"] = sal.line or 0
    except Exception:
        pass

    for reg in ("pc", "rsp", "rbp"):
        try:
            info[reg] = int(frame.read_register(reg))
        except Exception:
            info[reg] = 0

    # 栈帧范围：Windows x64 上栈向低地址增长，帧内从 rsp 向上到 rbp
    rsp, rbp = info["rsp"], info["rbp"]
    if rsp and rbp and rbp >= rsp:
        info["low"] = rsp
        info["high"] = rbp + 8  # 加上保存旧 rbp 的那个槽位
        info["size"] = info["high"] - rsp
    elif rsp:
        info["low"] = rsp
        info["high"] = rsp
        info["size"] = 0

    # 局部变量与参数
    try:
        blk = frame.block()
        depth = 0
        while blk is not None and depth < 12:
            # 走到文件作用域 / 全局作用域就停。
            #
            # 再往上是 static_block 和 global_block，那里的东西是全局/静态变量，
            # 由 _collect_globals 统一报告。以前一路走到底，于是 main 的帧变量里
            # 混进了 global_int 这类全局变量，变量面板里同一个变量出现两份
            # （一份标「全局」，一份标成「当前函数的局部变量」）。
            try:
                if blk.is_global or blk.is_static:
                    break
            except Exception:
                pass
            for sym in blk:
                if sym.is_argument or sym.is_variable:
                    scope = "arg" if sym.is_argument else "local"
                    entry = _collect_var(frame, sym, scope, index)
                    # 落在 .data / .bss / .rdata 的变量不在栈上（最典型的是
                    # 函数里的 static），同样归 _collect_globals 报告。
                    # 否则「当前函数」分组里会冒出一个地址在 .bss 的变量，
                    # 和栈帧范围对不上，看着像是采集错了。
                    if entry.get("storage") in ("data", "bss", "rdata"):
                        continue
                    info["vars"].append(entry)
            blk = blk.superblock
            depth += 1
    except Exception as exc:
        info["vars"].append(
            {"name": "<采集失败>", "issues": [str(exc)[:80]], "type": "", "size": 0,
             "address": None, "value": "", "scope": "local", "frame": index,
             "is_pointer": False, "pointer_target": None, "pointer_target_name": None,
             "storage": "unknown", "array_length": None, "is_string": False,
             "preview": None, "decl_line": None, "decl_func": None}
        )

    return info


# ---------------------------------------------------------------------------
# 函数作用域的 static 变量
# ---------------------------------------------------------------------------


def _storage_of_section(section):
    """把 `.bss` / `.data` 这类段名翻成教学用的 kind。"""
    name = (section or "").strip()
    if name in _SECTION_KIND:
        return _SECTION_KIND[name]
    if name.startswith(".bss"):
        return "bss"
    if name.startswith(".data"):
        return "data"
    if name.startswith(".rdata") or name.startswith(".rodata"):
        return "rdata"
    return "unknown"


def _enumerate_statics():
    """枚举本编译单元里所有「静态存储」的变量，**不依赖调用栈**。

    为什么不能靠 block 遍历？

    GDB 的 block 链只挂到 global_block / static_block（文件作用域）。函数里的
    `static int c;` 挂在该函数自己的 block 上，只有这个函数正在被调用、它的帧
    还在栈上时才走得到。于是停在 main 里就完全看不到 func 里的 c —— 明明它还
    活着、地址还在 .bss 里，指针也还指着它。

    `mt print symbols -source <file>` 是 GDB 的维护命令，按编译单元整份导出符号，
    输出形如：

        block #003, ..., function func
         int c; static at 0x7ff7f2387030 section .bss

    名字、地址、所在段一次给全，是绕开调用栈拿到它的唯一可行途径。
    结果按 (pid, 是否运行中) 缓存：一次运行内地址不会变，程序重启就必须重算。
    """
    running = _program_running()
    try:
        pid = int(gdb.selected_inferior().pid) if running else 0
    except Exception:
        pid = 0
    key = (pid, running)
    if _STATIC_CACHE["key"] == key:
        return _STATIC_CACHE["items"]

    items = []
    symtab = _find_symtab()
    text = ""
    if symtab is not None:
        try:
            # -source 要的是 symtab 里记的文件名（可能只是 "2.c"），不是绝对路径
            text = gdb.execute(
                'mt print symbols -source "%s"' % symtab.filename, to_string=True
            )
        except Exception as exc:
            _log("mt print symbols failed: %s" % exc)

    func = None
    for line in (text or "").splitlines():
        m = _BLOCK_FUNC_RE.search(line)
        if m:
            # 符号总是跟在它所属 block 的头之后，记住当前函数名即可归属
            func = m.group(1)
            continue
        m = _STATIC_SYM_RE.match(line)
        if not m:
            continue
        try:
            addr = int(m.group("addr"), 16)
        except ValueError:
            continue
        items.append(
            {
                "name": m.group("name"),
                "type": m.group("type").strip(),
                "dims": m.group("dims") or "",
                "address": addr,
                "section": m.group("section") or "",
                "func": func,
            }
        )

    _STATIC_CACHE["key"] = key
    _STATIC_CACHE["items"] = items
    if items:
        _log("statics found: %d" % len(items))
    return items


def _value_of_static(item):
    """按符号把静态变量的值读出来；读不到返回 None。

    三种写法依次尝试，全部要核对地址 —— 同名符号（不同函数里的 static 同名）
    很容易抓错对象，抓到别人的内存比读不出来更误导人。
    """
    name = item["name"]
    addr = item["address"]
    exprs = []
    if item.get("func"):
        # GDB 的限定名写法：'func'::c
        exprs.append("'%s'::%s" % (item["func"], name))
    exprs.append(name)
    if item.get("type"):
        dims = item.get("dims") or ""
        if dims:
            # 数组要写成 `char (*)[8]` 才对：`char *` 只解出第一个字节
            exprs.append("*(%s (*)%s)0x%X" % (item["type"], dims, addr))
        else:
            exprs.append("*(%s *)0x%X" % (item["type"], addr))
    for expr in exprs:
        try:
            value = gdb.parse_and_eval(expr)
        except Exception:
            continue
        try:
            if value.address is not None and int(value.address) != addr:
                continue
        except Exception:
            pass
        return value
    return None


def _collect_static_var(item):
    """把一个静态存储的符号采集成与全局变量同构的条目。"""
    entry = {
        "name": item["name"],
        "type": item["type"],
        "size": 0,
        "address": item["address"],
        "value": "",
        "scope": "static",
        "frame": None,
        "is_pointer": False,
        "pointer_target": None,
        "pointer_target_name": None,
        "storage": _storage_of_section(item.get("section")),
        "array_length": None,
        "is_string": False,
        "preview": None,
        "issues": [],
        "decl_line": None,
        "decl_func": item.get("func") or None,
    }
    if entry["storage"] == "unknown":
        _, kind = _locate(item["address"])
        if kind:
            entry["storage"] = kind

    value = _value_of_static(item)
    if value is not None:
        try:
            entry["type"] = str(value.type)
            entry["size"] = int(value.type.sizeof)
        except Exception:
            pass
        entry["value"] = _describe_value(value, _is_pointer(value.type))
        if _is_pointer(value.type):
            entry["is_pointer"] = True
            try:
                target = int(value)
                entry["pointer_target"] = target
            except Exception:
                target = None
            if target:
                if _is_char_pointer(value.type):
                    text = _read_cstring(target)
                    if text is not None:
                        entry["is_string"] = True
                        entry["preview"] = text
                _, kind = _locate(target)
                if kind:
                    entry["pointer_target_name"] = kind
        length = _array_length(value.type)
        if length is not None:
            entry["array_length"] = length

    # 值读不到（程序还没跑）时，至少把大小补上：变量卡片上「0B」比没有更误导
    if not entry["size"] and item.get("type"):
        try:
            entry["size"] = int(gdb.lookup_type(item["type"]).sizeof)
        except Exception:
            pass
    return entry


def _collect_globals():
    """采集全局变量与静态变量。

    三路并用：
    - 帧的 block 链向上走到 global/static block（实测能拿到静态变量）
    - 直接遍历 symtab 的 global_block / static_block（覆盖其它编译单元的全局变量）
    - `mt print symbols` 扫出函数作用域的 static（不要求该函数在调用栈上）
    """
    found = {}
    order = []

    def add(entry, key=None):
        key = key or entry.get("name")
        if key and key not in found:
            found[key] = entry
            order.append(key)

    # ---- 路径一：从当前帧的 block 链 ----
    try:
        frame = gdb.newest_frame()
        if frame is not None:
            frame.select()
            blk = frame.block()
            depth = 0
            while blk is not None and depth < 12:
                try:
                    is_global = bool(blk.is_global)
                    is_static = bool(blk.is_static)
                except Exception:
                    is_global = is_static = False
                if is_global or is_static:
                    scope = "global" if is_global else "static"
                    for sym in blk:
                        if sym.is_variable and not sym.is_argument:
                            add(_collect_var(frame, sym, scope, None))
                blk = blk.superblock
                depth += 1
    except Exception as exc:
        _log("globals via frame failed: %s" % exc)

    # ---- 路径二：遍历 symtab 的全局/静态块 ----
    try:
        symtab = _find_symtab()
        if symtab is not None:
            for blk, scope in ((symtab.global_block(), "global"), (symtab.static_block(), "static")):
                for sym in blk:
                    if sym.is_variable and not sym.is_argument:
                        entry = _collect_var_global(sym, scope)
                        if entry is not None:
                            add(entry)
    except Exception as exc:
        _log("globals via symtab failed: %s" % exc)

    # ---- 路径三：函数作用域的 static（不要求该函数在调用栈上）----
    #
    # 这是「停在 main 里，func 里的 static 就消失」的根因：它挂在 func 的
    # block 上，只有 func 正在被调用时才走得到。但它的生命期是整个程序，
    # 内存视图里 .bss 也确实有它 —— 变量面板却一片空白，初学者只能看到
    # 一个指向 .bss 的指针，无从知道那里住的是谁。
    try:
        for item in _enumerate_statics():
            addr = item["address"]
            prev = found.get(item["name"])
            if prev is not None and (prev.get("address") or 0) == addr:
                continue  # 上面两条路径已经采到同一个对象
            # 不同函数里的同名 static 是不同对象，键里带上地址才不会被去重掉
            add(_collect_static_var(item), "%s@%x" % (item["name"], addr))
    except Exception as exc:
        _log("statics via mt print failed: %s" % exc)

    return [found[k] for k in order]


def _find_symtab():
    """找到主源码文件的 symtab。"""
    try:
        frame = gdb.newest_frame()
        if frame is not None:
            frame.select()
            sal = frame.find_sal()
            if sal is not None and sal.symtab is not None:
                return sal.symtab
    except Exception:
        pass
    # 退路：用任意一个全局符号
    try:
        for name in ("main", "__main__"):
            sym = gdb.lookup_global_symbol(name)
            if sym is not None and sym.symtab is not None:
                return sym.symtab
    except Exception:
        pass
    return None


def _collect_var_global(sym, scope):
    """采集一个全局/静态变量（不依赖帧）。"""
    entry = {
        "name": sym.name,
        "type": "",
        "size": 0,
        "address": None,
        "value": "",
        "scope": scope,
        "frame": None,
        "is_pointer": False,
        "pointer_target": None,
        "pointer_target_name": None,
        "storage": "unknown",
        "array_length": None,
        "is_string": False,
        "preview": None,
        "issues": [],
        "decl_line": None,
        "decl_func": None,
    }
    try:
        v = sym.value()
    except Exception as exc:
        entry["issues"].append("无法读取: %s" % str(exc)[:60])
        return entry

    try:
        entry["decl_line"] = int(sym.line)
    except Exception:
        entry["decl_line"] = None

    try:
        entry["type"] = str(v.type)
        entry["size"] = int(v.type.sizeof)
    except Exception:
        pass
    try:
        addr = v.address
        entry["address"] = int(addr) if addr is not None else None
    except Exception:
        pass
    entry["value"] = _describe_value(v, _is_pointer(v.type))

    if _is_pointer(v.type):
        entry["is_pointer"] = True
        try:
            target = int(v)
            entry["pointer_target"] = target
        except Exception:
            target = None
        if target:
            if _is_char_pointer(v.type):
                text = _read_cstring(target)
                if text is not None:
                    entry["is_string"] = True
                    entry["preview"] = text
            _, kind = _locate(target)
            if kind:
                entry["pointer_target_name"] = kind

    length = _array_length(v.type)
    if length is not None:
        entry["array_length"] = length

    if entry["address"] is not None:
        _, kind = _locate(entry["address"])
        if kind:
            entry["storage"] = kind

    return entry


# ---------------------------------------------------------------------------
# 内存字节窗口
# ---------------------------------------------------------------------------


def _load_heap_blocks():
    """读堆账本，得到当前还活着的堆块。

    账本由插桩后的被调试程序写出（地址用 %p 的十六进制格式，
    大小用十进制）。这里解析出「地址 -> (大小, 来源文件, 来源行号)」。
    """
    path = os.environ.get("CDBG_HEAP_LOG", "")
    if not path or not os.path.isfile(path):
        return {}

    blocks = {}
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                parts = raw.split()
                if not parts:
                    continue
                kind = parts[0]
                try:
                    if kind == "M" and len(parts) >= 3:
                        addr = int(parts[1], 16)
                        size = int(parts[2], 10)
                        file = parts[3] if len(parts) > 3 else ""
                        line = int(parts[4], 10) if len(parts) > 4 else 0
                        blocks[addr] = (size, file, line)
                    elif kind == "F" and len(parts) >= 2:
                        blocks.pop(int(parts[1], 16), None)
                    elif kind == "R" and len(parts) >= 4:
                        blocks.pop(int(parts[1], 16), None)
                        blocks[int(parts[2], 16)] = (
                            int(parts[3], 10),
                            parts[4] if len(parts) > 4 else "",
                            int(parts[5], 10) if len(parts) > 5 else 0,
                        )
                except (ValueError, IndexError):
                    continue
    except Exception as exc:
        _log("heap ledger read failed: %s" % exc)

    return blocks


def _current_func_entry():
    """当前函数在程序段里的入口地址（取不到返回 None）。"""
    try:
        frame = gdb.newest_frame()
        if frame is None:
            return None
        sym = frame.function()
        if sym is None:
            return None
        addr = sym.value().address
        return int(addr) if addr is not None else None
    except Exception:
        return None


def _collect_chunks(frames, globals_):
    """采集需要以 HEX 展示的字节区域。

    - 每个栈帧的完整字节
    - 全局/静态变量覆盖的字节（合并相邻区间）
    - 字符串字面量所在位置
    - 堆块的实际内容
    """
    chunks = []
    seen = set()

    def add_chunk(kind, start, data, label, owner=None):
        if not data:
            return
        key = (kind, start)
        if key in seen:
            return
        seen.add(key)
        chunks.append(
            {"kind": kind, "start": start, "data": data, "label": label, "owner": owner}
        )

    # ---- 堆块 ----
    for addr, (size, file, line) in _load_heap_blocks().items():
        if size <= 0:
            continue
        hexdata, got = _read_bytes(addr, min(size, 512))
        if got:
            add_chunk(
                "heap",
                addr,
                hexdata,
                "堆块 %d 字节 @ %s:%d" % (size, file, line),
            )

    # ---- 程序段：当前函数的机器指令 ----
    #
    # .text 一直是可读的（r-x），读它不费事。不读的话「程序段」分区就只剩一个
    # 地址范围 —— 面板画得出来，后面却是空的，看着像坏了一样。
    # 只取入口处 96 字节：整段有几 KB，全画出来既没用也拖慢渲染，而入口那几行
    # 指令正好对上「你写的第一句代码变成了什么」。
    if _program_running():
        entry = _current_func_entry()
        if entry:
            hexdata, got = _read_bytes(entry, 96)
            if got:
                add_chunk(
                    "text",
                    entry,
                    hexdata,
                    "程序段：%s() 的机器指令（入口起 %d 字节，只读不可改）"
                    % (frames[0]["func"] if frames else "?", got),
                )

    # ---- 栈帧 ----
    for fr in frames:
        if fr["size"] <= 0:
            continue
        hexdata, got = _read_bytes(fr["low"], fr["size"])
        if got:
            add_chunk(
                "stack",
                fr["low"],
                hexdata,
                "栈帧 #%d  %s()" % (fr["index"], fr["func"]),
            )

    # ---- 全局与静态变量：按区域分组，读整段 ----
    by_kind = {}

    def add_var(addr, size, name, kind):
        if not addr or not size:
            return
        if kind in ("unknown", "text", None):
            kind = "data"
        by_kind.setdefault(kind, []).append((addr, size, name))

    for g in globals_:
        add_var(g.get("address"), g.get("size"), g.get("name"), g.get("storage"))

    # 函数内的 static 变量（以及文件作用域的 static）不在 globals_ 里 ——
    # GDB 的 block 遍历只给 global_block / static_block，函数作用域的静态变量
    # 挂在函数自己的 block 上，拿不到。但它们确实占 .data / .bss 的空间，
    # 不补进来的话，内存视图里这块就是空的（变量面板却有地址，很割裂）。
    for fr in frames:
        for v in fr.get("vars", []):
            if v.get("storage") not in ("data", "bss", "rdata"):
                continue
            add_var(
                v.get("address"), v.get("size"), v.get("name"),
                v.get("storage"),
            )

    for kind, items in by_kind.items():
        items.sort()
        # 合并相邻/重叠区间
        merged = []
        for addr, size, name in items:
            end = addr + size
            if merged and addr <= merged[-1][1] + 16:
                prev = merged[-1]
                prev[1] = max(prev[1], end)
                if name not in prev[2]:     # 同一个变量可能既在 globals 又在帧里
                    prev[2].append(name)
            else:
                merged.append([addr, end, [name]])

        for lo, hi, names in merged:
            hexdata, got = _read_bytes(lo, hi - lo)
            if got:
                # 标签带上真实段名（.data / .bss）：合并成一个分区之后，
                # 用户需要一眼看出这块字节是「已初始化」还是「初值为 0」的那段。
                sec_name, _kind = _locate(lo)
                add_chunk(
                    kind,
                    lo,
                    hexdata,
                    "%s：%s" % (sec_name or _kind_label(kind), "、".join(names[:6])),
                    names[0] if len(names) == 1 else None,
                )

    # ---- 字符串字面量（指针指向的只读数据）----
    str_targets = set()
    for g in globals_:
        if g.get("is_string") and g.get("pointer_target"):
            str_targets.add(g["pointer_target"])
    for fr in frames:
        for v in fr.get("vars", []):
            if v.get("is_string") and v.get("pointer_target"):
                str_targets.add(v["pointer_target"])

    for target in sorted(str_targets):
        sec, kind = _locate(target)
        if kind != "rdata":
            continue
        text = _read_cstring(target)
        if text is None:
            continue
        size = len(text.encode("utf-8", errors="replace")) + 1
        hexdata, got = _read_bytes(target, size)
        if got:
            add_chunk("rdata", target, hexdata, '字符串常量 "%s"' % text)

    return chunks


def _kind_label(kind):
    return {
        "text": "程序段",
        "data": "数据段",
        "rdata": "只读数据段",
        "bss": "初值为 0 的数据段",
        "heap": "堆",
        "stack": "栈",
    }.get(kind, kind)


# ---------------------------------------------------------------------------
# 主采集流程
# ---------------------------------------------------------------------------


def _collect():
    """采集一份完整快照。"""
    result = {
        "state": "stopped",
        "reason": "",
        "pid": None,
        "program": "",
        "current_file": "",
        "current_line": 0,
        "current_func": "",
        "frames": [],
        "globals": [],
        "chunks": [],
        "regions": [],
        "depth": 0,
        "stack_bytes": 0,
        "warnings": [],
    }

    try:
        inferior = gdb.selected_inferior()
        result["pid"] = inferior.pid
    except Exception:
        pass

    try:
        result["program"] = gdb.current_progspace().filename or ""
    except Exception:
        pass

    # ---- 帧链 ----
    frames = []
    try:
        frame = gdb.newest_frame()
        index = 0
        while frame is not None:
            frame.select()
            frames.append(_collect_frame(frame, index))
            frame = frame.older()
            index += 1
            if index > 64:
                break
    except Exception as exc:
        result["warnings"].append("栈帧采集失败: %s" % str(exc)[:80])

    result["frames"] = frames
    result["depth"] = len(frames)
    result["stack_bytes"] = sum(f["size"] for f in frames)

    if frames:
        result["current_file"] = frames[0]["file"]
        result["current_line"] = frames[0]["line"]
        result["current_func"] = frames[0]["func"]

    # ---- 全局与静态变量 ----
    globals_ = _collect_globals()
    result["globals"] = globals_

    # ---- 段 ----
    result["regions"] = _parse_sections()

    # ---- 字节 ----
    try:
        result["chunks"] = _collect_chunks(frames, globals_)
    except Exception as exc:
        result["warnings"].append("内存读取失败: %s" % str(exc)[:80])

    return result


# ---------------------------------------------------------------------------
# 事件挂钩
# ---------------------------------------------------------------------------


def _on_stop(event):
    """程序每次停下来就采集一次。"""
    try:
        gdb.execute("set pagination off", to_string=True)
        gdb.execute("set confirm off", to_string=True)
    except Exception:
        pass
    try:
        result = _collect()
        result["reason"] = _reason_of(event)
        _write_json(result)
    except Exception as exc:
        _log("collect failed: %s" % exc)
        try:
            _write_json(
                {
                    "state": "stopped",
                    "reason": "collect-error",
                    "frames": [],
                    "globals": [],
                    "chunks": [],
                    "regions": [],
                    "warnings": ["采集异常: %s" % str(exc)[:120]],
                }
            )
        except Exception:
            pass


def _reason_of(event):
    try:
        if event is None:
            return "stopped"
        if getattr(event, "breakpoint", None) is not None:
            return "breakpoint-hit"
        if getattr(event, "stop_signal", None) is not None:
            return "signal"
        return "step"
    except Exception:
        return "stopped"


def _on_exit(event):
    """程序退出。"""
    try:
        _write_json(
            {
                "state": "exited",
                "reason": "exited",
                "pid": getattr(event, "exit_code", None),
                "program": "",
                "current_file": "",
                "current_line": 0,
                "current_func": "",
                "frames": [],
                "globals": [],
                "chunks": [],
                "regions": [],
                "depth": 0,
                "stack_bytes": 0,
                "warnings": ["程序已退出"],
            }
        )
    except Exception:
        pass


def _on_continue(event):
    """程序继续运行 —— 清空缓存的段信息（理论上不会变，但保险）。"""
    return


gdb.events.stop.connect(_on_stop)
gdb.events.exited.connect(_on_exit)


class CDBGCollectCommand(gdb.Command):
    """手动触发一次采集（用于主进程兜底重试）。"""

    def __init__(self):
        super(CDBGCollectCommand, self).__init__("cdbg-collect", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        result = _collect()
        _write_json(result)


CDBGCollectCommand()
_log("probe ready, out=%s" % OUT_PATH)
