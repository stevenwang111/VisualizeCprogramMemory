"""编译与工作目录管理。

两个关键职责：

1. **编译**：用适合教学调试的 flags 把 .c 编成 exe。
   `-fno-inline` 尤其重要 —— 开了内联，一行源码会对应多条机器指令，
   初学者按「单步」时行号会跳着走，整个学习体验就崩了。

2. **ASCII 工作目录**：GDB 用 GBK 解码源码路径，遇到中文目录会直接抛
   UnicodeDecodeError 崩溃（实测确认）。所以我们把源码复制到一个纯 ASCII
   的临时工作目录里再编译，GDB 永远只看到 ASCII 路径。
   源码面板仍然显示用户的原始文件内容与路径。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import toolchain_config

#: 教学调试专用的编译选项
DEBUG_FLAGS = [
    "-g",  # 生成调试信息（DWARF）—— 没有它 GDB 看不到变量
    "-O0",  # 关闭优化，保持源码与机器指令一一对应
    "-fno-omit-frame-pointer",  # 保留帧指针，让栈帧边界可精确推导
    "-fno-stack-protector",  # 关掉栈保护，避免 canary 干扰栈布局
    "-fno-inline",  # 禁止内联，单步时行号不跳
    "-fno-strict-aliasing",  # 放宽别名规则，避免优化带来的怪现象
    "-fno-common",  # 多个 .c 文件的同名全局不再被合并，初学者更好理解
]

#: 常见安装位置（Windows 上的 MSYS2 / MinGW）——
#: 只在「配置没有 / 配置里的路径失效 / 扫描也没结果」时才用到，
#: 正常情况走 `config/toolchain.conf`（由 compiler_scanner 生成）。
_COMPILER_CANDIDATES = [
    r"C:\msys64\ucrt64\bin\gcc.exe",
    r"C:\msys64\mingw64\bin\gcc.exe",
    r"C:\msys64\usr\bin\gcc.exe",
    r"C:\MinGW\bin\gcc.exe",
    r"C:\mingw64\bin\gcc.exe",
    r"C:\ProgramData\mingw64\mingw64\bin\gcc.exe",
]

#: 常见 GDB 位置
_GDB_CANDIDATES = [
    r"C:\msys64\ucrt64\bin\gdb.exe",
    r"C:\msys64\mingw64\bin\gdb.exe",
    r"C:\msys64\usr\bin\gdb.exe",
    r"C:\MinGW\bin\gdb.exe",
    r"C:\mingw64\bin\gdb.exe",
    r"C:\ProgramData\mingw64\mingw64\bin\gdb.exe",
]


class BuildError(RuntimeError):
    """编译失败。

    diagnostics 里带着定位信息（文件、行号、消息），
    供界面在源码上标出出错的位置。
    """

    def __init__(self, message: str, diagnostics: Optional[List[Dict]] = None):
        super().__init__(message)
        self.diagnostics = diagnostics or []


class ToolNotFound(RuntimeError):
    """找不到 gcc / gdb。"""


@dataclass
class BuildResult:
    """一次编译的结果。"""

    ok: bool
    exe_path: str
    """编译产出的 exe 路径（位于 ASCII 工作目录）。"""

    source_copy: str
    """复制到工作目录的源码副本路径。"""

    original_source: str
    """用户原始源码路径。"""

    workdir: str
    command: List[str] = field(default_factory=list)
    stderr: str = ""
    stdout: str = ""
    errors: List[Dict] = field(default_factory=list)
    """解析出来的编译错误 / 警告，含行号。

    初学者最需要知道的就是「我错在哪一行」——
    把 GCC 的报错定位到源码行上，比贴一整屏编译日志有用得多。
    """

    @property
    def exe_name(self) -> str:
        return os.path.basename(self.exe_path)


#: 匹配 GCC 的诊断行：  文件:行:列: 级别: 消息
#:
#: 注意文件路径里带盘符冒号（"C:\path\file.c:5:21: error: ..."），
#: 所以文件名部分必须用非贪婪的 .+?，让它一路退到最后一个能对上
#: "行号[:列号]: 级别:" 的位置为止。
_DIAG_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<col>\d+))?:\s*"
    r"(?P<level>fatal error|error|warning):\s*(?P<msg>.*)$"
)


def find_symbol_definers(source: str, workdir: str, symbols: List[str],
                          max_files: int = 6) -> Dict[str, List[str]]:
    """找出「定义」了这些符号的兄弟 .c 文件。

    返回 {文件路径: [该文件定义的符号]}。
    用 nm 判断而不是正则 —— `func();` 这样的调用长得和 `int func() {`
    很像，光看源码文本会认错。
    """
    nm = shutil.which("nm")
    cc = find_compiler()
    if not nm:
        return []

    src_dir = os.path.dirname(os.path.abspath(source))
    if not os.path.isdir(src_dir):
        return []

    inc = ["-I", src_dir] if os.path.isdir(src_dir) else []
    siblings = []
    for name in sorted(os.listdir(src_dir)):
        if name.lower().endswith(".c"):
            sib = os.path.join(src_dir, name)
            if os.path.normcase(sib) != os.path.normcase(os.path.abspath(source)):
                siblings.append(sib)

    def defines(path, sym):
        """判断某文件是否定义了该符号。

        返回 "global"（外部可见）/ "static"（static 修饰，外部访问不到）/ None。
        nm 用大小写区分：T/D/B 是全局符号，t/d/b 是文件内（static）符号。
        """
        obj2 = os.path.join(
            workdir, "_chk_%s.o" % _ascii_safe(os.path.basename(path))[:20]
        )
        try:
            p = subprocess.run(
                [cc, "-c", "-g", "-O0", *inc, path, "-o", obj2],
                cwd=workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            if p.returncode != 0 or not os.path.isfile(obj2):
                return None
            p = subprocess.run(
                [nm, "--defined-only", obj2],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        for raw in p.stdout.splitlines():
            parts = raw.split()
            # nm --defined-only 的格式是「地址 类型字母 符号名」三列，
            # 符号名在最后 —— 取最后一段才不会把类型字母当成符号名。
            if len(parts) >= 3 and parts[-1] == sym:
                kind = parts[-2]
                return "global" if kind.isupper() else "static"
        return None

    # 贪心选：每选一个文件，只用它覆盖「还没被覆盖」的符号。
    # 否则两个文件都定义了同名符号（比如本目录里有多个练习文件），
    # 一起编译会撞成重复定义。
    scored = []
    for sib in siblings:
        hits = []
        static_hits = []
        for s in symbols:
            kind = defines(sib, s)
            if kind == "global":
                hits.append(s)
            elif kind == "static":
                static_hits.append(s)
        if hits:
            scored.append((len(hits), sib, hits, static_hits))
    scored.sort(key=lambda x: x[0], reverse=True)

    chosen = {}
    covered = set()
    for hits, sib, syms, static_hits in scored:
        if len(chosen) >= max_files:
            break
        fresh = [s for s in syms if s not in covered]
        if not fresh:
            continue
        chosen[sib] = fresh
        covered.update(fresh)

    # static 符号单独记一份：它们不是「漏编译」，而是「根本不能跨文件用」
    static_found = {}
    for sib in siblings:
        for s in symbols:
            if s in covered:
                continue
            if defines(sib, s) == "static":
                static_found.setdefault(s, []).append(sib)

    return chosen, static_found


def collect_undefined_symbols(source: str, workdir: str, target: str) -> List[str]:
    """编译成 .o，用 nm 列出未定义的符号（去掉 CRT 噪音）。"""
    nm = shutil.which("nm")
    if not nm:
        return []
    cc = find_compiler()
    src_dir = os.path.dirname(os.path.abspath(source))
    inc = ["-I", src_dir] if os.path.isdir(src_dir) else []
    obj = os.path.join(workdir, "_linkcheck.o")
    try:
        proc = subprocess.run(
            [cc, "-c", "-g", "-O0", *inc, target, "-o", obj],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if proc.returncode != 0 or not os.path.isfile(obj):
            return []
        proc = subprocess.run(
            [nm, "-u", obj],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    undefined = []
    for raw in proc.stdout.splitlines():
        parts = raw.split()
        if len(parts) >= 2 and parts[0] in ("U", "w"):
            undefined.append(parts[1])

    crt_noise = ("__", "_", "printf", "scanf", "puts", "malloc", "free",
                 "memcpy", "memset", "strlen", "strcpy", "mainCRTStartup")
    return [s for s in undefined if not any(s.startswith(p) for p in crt_noise)]


def diagnose_link_failure(source: str, workdir: str, compile_target: str = "") -> List[Dict]:
    """链接失败时做一次主动诊断。

    MinGW 的 collect2 常常只吐一句 "ld returned 1 exit status"，
    把真正的原因（缺 main、符号没定义、漏编译了别的 .c）全吞了。
    对初学者来说这条信息等于没说，所以这里自己查一遍。
    """
    hints: List[Dict] = []
    target = compile_target or source

    text = _read_source_text(source)
    # ---- 1. 有没有 main ----
    if not re.search(r"\bmain\s*\(", text):
        hints.append(
            {
                "file": source,
                "line": 0,
                "col": 0,
                "level": "error",
                "message": "链接失败：源文件里找不到 main() 函数。"
                            "链接器需要 main 作为程序入口 —— "
                            "检查是不是写成了 Main、main 拼错，或者函数名漏了 int。",
            }
        )
        return hints

    # ---- 2. 有哪些符号没定义 ----
    undefined = collect_undefined_symbols(source, workdir, target)
    if not undefined:
        return hints

    # ---- 3. 这些符号在别的 .c 文件里定义了吗 ----
    definers, static_found = find_symbol_definers(source, workdir, undefined)

    for sym in undefined[:6]:
        # 先看是不是「static 符号跨文件访问」这类语义错误
        if sym in static_found:
            where = static_found[sym]
            hints.append(
                {
                    "file": source,
                    "line": 0,
                    "col": 0,
                    "level": "error",
                    "message": "链接失败：符号 `%s` 在 %s 里是用 static 定义的 —— "
                                "static 变量（函数）只能在**定义它的那个 .c 文件里**使用，"
                                "别的文件即使 extern 声明了也链接不到。\n"
                                "要跨文件共享，去掉它的 static；或者把它改成函数 / 用参数传进去。"
                                % (sym, os.path.basename(where[0])),
                }
            )
            continue

        where = [path for path, syms in definers.items() if sym in syms]
        if len(where) == 1:
            hints.append(
                {
                    "file": source,
                    "line": 0,
                    "col": 0,
                    "level": "error",
                    "message": "链接失败：符号 `%s` 未定义。它定义在 %s 里 —— "
                                "这两个文件要一起编译（gcc %s %s -o prog）。"
                                % (sym, os.path.basename(where[0]),
                                   os.path.basename(source), os.path.basename(where[0])),
                }
            )
        elif len(where) > 1:
            names = "、".join(os.path.basename(p) for p in where)
            hints.append(
                {
                    "file": source,
                    "line": 0,
                    "col": 0,
                    "level": "error",
                    "message": "链接失败：符号 `%s` 未定义。注意 %s 里都有同名符号 —— "
                                "它们未必是同一个东西（本目录里可能有多个练习文件）。"
                                "确认该用哪一份，然后把它们和本文件一起编译。"
                                % (sym, names),
                }
            )
        else:
            hints.append(
                {
                    "file": source,
                    "line": 0,
                    "col": 0,
                    "level": "error",
                    "message": "链接失败：符号 `%s` 未定义。"
                                "检查是否漏写了它的定义，或者它被声明成了 static "
                                "（static 变量只能在同一个 .c 文件里用）。" % sym,
                }
            )

    return hints


def parse_diagnostics(text: str, workdir: str = "") -> List[Dict]:
    """从编译器输出里解析出定位信息。"""
    out: List[Dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _DIAG_RE.match(line)
        if not m:
            continue
        level = m.group("level")
        if level == "fatal error":
            level = "error"
        if level == "note":
            continue
        path = m.group("file")
        # GCC 有时给的是相对路径或带引号的路径
        path = path.strip().strip('"')
        if workdir and not os.path.isabs(path):
            candidate = os.path.join(workdir, path)
            if os.path.isfile(candidate):
                path = candidate
        out.append(
            {
                "file": path,
                "line": int(m.group("line")),
                "col": int(m.group("col")) if m.group("col") else 0,
                "level": level,
                "message": m.group("msg").strip(),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 工具定位
# ---------------------------------------------------------------------------


def _which(name: str) -> Optional[str]:
    path = shutil.which(name)
    return path


def find_compiler() -> str:
    """定位 gcc。

    优先级：环境变量 CDBG_CC > config/toolchain.conf 里选中的那套 >
    PATH > 常见安装目录。

    配置文件那一档是「首次开箱」的关键：配置缺失、或者按配置里的路径
    找不到 gcc（换了设备 / 卸载重装 / 挪了盘符），`toolchain_config`
    会自动扫一遍本机并把结果写回配置，所以同一份代码拷到别的机器也能跑。
    """
    env = os.environ.get("CDBG_CC")
    if env and os.path.isfile(env):
        return env

    configured = toolchain_config.gcc_path()
    if configured:
        return configured

    for name in ("gcc", "x86_64-w64-mingw32-gcc"):
        found = _which(name)
        if found:
            return found
    for cand in _COMPILER_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    raise ToolNotFound(
        "找不到 gcc。请安装 MSYS2 UCRT64 / MinGW-w64，"
        "或运行 `python cdbg.py --scan-compiler` 让程序自己找一套，"
        "也可以设置环境变量 CDBG_CC 指向 gcc.exe 的完整路径。"
    )


def find_gdb() -> str:
    """定位 gdb。

    同样先看配置：**gdb 要和 gcc 来自同一套工具链**，混用两个发行版
    的 gcc/gdb 时符号格式、运行时库都可能对不上，症状是「能编译、
    一调试就报错」或者变量全空。
    """
    env = os.environ.get("CDBG_GDB")
    if env and os.path.isfile(env):
        return env

    configured = toolchain_config.gdb_path()
    if configured:
        return configured

    # 退一步：跟 gcc 放同一个 bin 目录的 gdb（配置里没写 gdb 字段时）
    try:
        sibling = os.path.join(os.path.dirname(find_compiler()), "gdb.exe")
        if os.path.isfile(sibling):
            return sibling
    except ToolNotFound:
        pass

    for name in ("gdb", "x86_64-w64-mingw32-gdb"):
        found = _which(name)
        if found:
            return found
    for cand in _GDB_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    raise ToolNotFound(
        "找不到 gdb。请安装 MSYS2 UCRT64 / MinGW-w64，"
        "或运行 `python cdbg.py --scan-compiler`，"
        "也可以设置环境变量 CDBG_GDB 指向 gdb.exe 的完整路径。"
    )


# ---------------------------------------------------------------------------
# ASCII 工作目录
# ---------------------------------------------------------------------------


def _ascii_safe(text: str) -> str:
    """把任意文本转成只含 ASCII 字母数字和下划线的形式。"""
    out = []
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in "_-."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "x"


def make_workspace(original_path: str, root: Optional[str] = None) -> str:
    """为一次调试创建一个纯 ASCII 的工作目录。

    目录名用源文件路径的哈希，避免不同文件互相覆盖，同时保证名字里
    没有中文 —— 因为 GDB 要在这个目录里工作。

    哈希后面再缀一个进程标识：同一份源码被同时打开两次时（比如用户开了
    两个窗口），两个会话会抢同一个 exe 文件，编译直接失败 ——
    「cannot open output file ... Permission denied」。
    加了后缀就彻底互不干扰。
    """
    if root is None:
        root = os.path.join(tempfile.gettempdir(), "cdbg_ws")
    os.makedirs(root, exist_ok=True)

    abs_path = os.path.abspath(original_path)
    digest = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:10]
    stem = _ascii_safe(os.path.splitext(os.path.basename(abs_path))[0])[:24] or "prog"

    workdir = os.path.join(root, "%s_%s_%d" % (stem, digest, os.getpid()))
    os.makedirs(workdir, exist_ok=True)
    return workdir


def is_ascii_path(path: str) -> bool:
    """判断路径是否完全由 ASCII 字符组成。"""
    try:
        path.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


# ---------------------------------------------------------------------------
# 编译
# ---------------------------------------------------------------------------


def _read_source_text(path: str) -> str:
    """读源码，容忍各种编码（BOM / GBK / UTF-8）。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def compile_source(
    source: str,
    workdir: Optional[str] = None,
    extra_flags: Optional[List[str]] = None,
    extra_sources: Optional[List[str]] = None,
    timeout: int = 120,
    override_text: Optional[str] = None,
) -> BuildResult:
    """把 .c 编译成 exe。

    source 必须是 .c 文件；.exe 直接进入调试（不编译）。
    override_text 用于插桩：直接用给定的文本作为工作副本的内容。
    """
    source = os.path.abspath(source)
    if not os.path.isfile(source):
        raise BuildError("源码文件不存在：%s" % source)

    if source.lower().endswith(".exe"):
        # 已经是可执行文件，直接用（用户自备 -g 编译）
        return BuildResult(
            ok=True,
            exe_path=source,
            source_copy=source,
            original_source=source,
            workdir=os.path.dirname(source),
        )

    if workdir is None:
        workdir = make_workspace(source)

    stem = _ascii_safe(os.path.splitext(os.path.basename(source))[0])[:32] or "prog"
    copy_path = os.path.join(workdir, stem + ".c")
    exe_path = os.path.join(workdir, stem + ".exe")

    # 源码副本：统一写成 UTF-8，避免 GDB 读取时的编码问题
    text = override_text if override_text is not None else _read_source_text(source)
    with open(copy_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)

    # 附加源文件（如插桩 sidecar）也要复制进工作目录
    copied_extras = []
    for extra in extra_sources or []:
        if not os.path.isfile(extra):
            continue
        extra_stem = _ascii_safe(os.path.splitext(os.path.basename(extra))[0])[:32] or "extra"
        dst = os.path.join(workdir, extra_stem + ".c")
        # 插桩模块可能已经把文件放好了 —— 源和目标相同就别再复制
        try:
            if os.path.samefile(extra, dst):
                copied_extras.append(dst)
                continue
        except OSError:
            pass
        shutil.copyfile(extra, dst)
        copied_extras.append(dst)

    cc = find_compiler()
    cmd = [cc, *DEBUG_FLAGS, *(extra_flags or [])]

    # 把源文件原来的目录加入头文件搜索路径。
    # 源码会被复制到 ASCII 工作目录再编译，#include "xxx.h" 指向的本地
    # 头文件在那里找不到 —— 用 -I 指回原目录最稳妥：
    # 既不用复制（避免中文文件名被改名后 include 失效），
    # 也天然支持多文件项目。
    src_dir = os.path.dirname(source)
    if src_dir and os.path.isdir(src_dir):
        cmd += ["-I", src_dir]

    cmd += [copy_path, *copied_extras, "-o", exe_path]

    try:
        proc = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise BuildError("编译超时（%d 秒）" % timeout)
    except OSError as exc:
        raise BuildError("无法执行编译器：%s" % exc)

    if proc.returncode != 0 or not os.path.isfile(exe_path):
        diagnostics = parse_diagnostics(proc.stderr or proc.stdout or "", workdir)
        # 链接错误（ld returned N）通常不带文件名行号，单独标一条
        if not diagnostics:
            fallback = ""
            for raw in (proc.stderr or proc.stdout or "").splitlines():
                line = raw.strip()
                if not line:
                    continue
                # 优先挑真正含 error 的那行，而不是 "In function 'main':" 这种
                if "error" in line.lower():
                    fallback = line
                    break
                if not fallback:
                    fallback = line
            if fallback:
                diagnostics.append(
                    {
                        "file": source,
                        "line": 0,
                        "col": 0,
                        "level": "error",
                        "message": fallback,
                    }
                )
            # 链接器错误信息太笼统时，主动查一遍真正的原因。
            # 查出来的话，用更具体的诊断替换掉那句没营养的 collect2 报错。
            if "ld returned" in fallback:
                try:
                    detailed = diagnose_link_failure(source, workdir, copy_path)
                except Exception:
                    detailed = []
                if detailed:
                    diagnostics = detailed
        raise BuildError(
            "编译失败：\n%s" % (proc.stderr or proc.stdout or "(无错误输出)"),
            diagnostics=diagnostics,
        )

    return BuildResult(
        ok=True,
        exe_path=exe_path,
        source_copy=copy_path,
        original_source=source,
        workdir=workdir,
        command=cmd,
        stderr=proc.stderr or "",
        stdout=proc.stdout or "",
    )


def toolchain_info() -> dict:
    """收集工具链信息，界面上展示给用户（排查环境问题用）。"""
    info = {
        "compiler": None,
        "gdb": None,
        "errors": [],
        "config": toolchain_config.CONFIG_PATH,
        "selected": toolchain_config.read_selected().get("名称", ""),
    }
    try:
        info["compiler"] = find_compiler()
    except ToolNotFound as exc:
        info["errors"].append(str(exc))
    try:
        info["gdb"] = find_gdb()
    except ToolNotFound as exc:
        info["errors"].append(str(exc))
    return info
