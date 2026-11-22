#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
compiler_scanner.py —— GCC/G++ 编译器搜刮模块

做的事情只有一件：把本机上零散安装的 C/C++ 工具链找出来，记进
``<项目根>/config/toolchain.conf``，然后挑一套作为「当前编译器」。

它原来是独立的工具，现在直接长在 cdbg 包里（``cdbg/compiler_scanner.py``）：
- `toolchain_config.py` 用 `scan_all()` / `write_config()` 做**首次开箱**的自动探测；
- 想手动挑一套就跑 `python cdbg.py --scan-compiler`（TUI 交互）。

本模块刻意不 import 主程序的任何模块（只被主程序单向依赖），
删掉 `toolchain_config.py` 的调用，cdbg 照常按 PATH 找编译器。

用法
----
    python -m cdbg.compiler_scanner              # 扫描 + TUI 选择
    python -m cdbg.compiler_scanner --no-tui     # 只扫描，写入配置后退出
    python -m cdbg.compiler_scanner --select 2   # 非交互，直接选第 2 套
    python -m cdbg.compiler_scanner --json       # 以 JSON 输出扫描结果
    python -m cdbg.compiler_scanner --install    # 补装一套 MinGW-w64

「可用」的判据是「编译出来的程序 GDB 能不能读到 DWARF 调试信息」，不是
「能不能编译」：MSVC 的 cl.exe、不带 Python 的 gdb 都判为不可用，扫描结果里
照旧列出来（说明情况），但不会被自动选中，而是引导去补装。

补装的下载走 GitHub Releases。系统区域在中国大陆时会先问一句「要不要加速」，
并在界面上写明加速服务来源（gh-proxy.org，第三方公共反代，非本项目提供）；
不想走第三方就答否，或设 CDBG_NO_ACCELERATE=1。

配置文件的结构
--------------
    [概况]                     扫描时间、数量
    [设置项1-扫描到的编译器]   每条被搜到的工具链一行摘要
    [编译器01] ...             每套工具链的完整字段（版本、头文件、gcc/gdb...）
    [设置项2-当前编译器]       选中的那套，字段与上面一致

「设置项2」就是选择结果落盘的位置，每次确认都会重写它。
**主程序只读「设置项2」**（见 `toolchain_config.read_selected`）。
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

IS_WIN = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: 项目根目录（放 cdbg.py 的地方）。cdbg/compiler_scanner.py -> 上两级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 配置文件固定落在 <项目根>/config/toolchain.conf ——
#: 与启动时的 cwd 无关，所以从任何目录 `cdbg xxx.c` 都读同一份配置。
CONFIG_DIR_NAME = "config"
CONFIG_FILE_NAME = "toolchain.conf"
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, CONFIG_DIR_NAME, CONFIG_FILE_NAME)

# 要检查的基础头文件：C 标准库 + C++ 标准库（Windows 额外检查 Windows API）
if IS_WIN:
    HEADERS = [
        "windows.h", "stdio.h", "stdlib.h", "string.h", "math.h",
        "assert.h", "time.h", "stdint.h",
        "iostream", "vector", "string", "algorithm", "cstdio", "cstring",
    ]
    CORE_HEADERS = ("windows.h", "stdio.h", "iostream")
else:
    HEADERS = [
        "stdio.h", "stdlib.h", "string.h", "math.h",
        "assert.h", "time.h", "stdint.h",
        "iostream", "vector", "string", "algorithm", "cstdio", "cstring",
    ]
    CORE_HEADERS = ("stdio.h", "stdlib.h", "string.h")

# 截图里 Dev-C++「程序」页的六个工具名，配置字段与之对齐
if IS_WIN:
    TOOL_CANDIDATES = {
        "gcc":     ("gcc.exe",),
        "g++":     ("g++.exe", "g++-15.exe", "g++-14.exe", "g++-13.exe", "g++-12.exe"),
        "make":    ("make.exe", "mingw32-make.exe"),
        "gdb":     ("gdb.exe",),
        "nm":      ("nm.exe",),
        "windres": ("windres.exe",),
        "gprof":   ("gprof.exe",),
    }
    EXE_HINTS = (
        "gcc.exe", "g++.exe", "clang.exe", "clang++.exe", "cl.exe", "avr-gcc.exe",
    )
else:
    TOOL_CANDIDATES = {
        "gcc":     ("gcc", "cc", "clang"),
        "g++":     ("g++", "c++", "clang++"),
        "make":    ("make", "gmake"),
        "gdb":     ("gdb",),
        "nm":      ("nm",),
        "windres": (),
        "gprof":   ("gprof",),
    }
    EXE_HINTS = ("gcc", "cc", "clang", "g++", "c++", "clang++")

#: 默认补装包：winlibs 的 MinGW-w64（gcc 16.1 + gdb 17.2，带 Python）。
#: 为什么是这一份：
#: - 压缩包是 **zip**，标准库的 zipfile 直接能解，不需要第三方 7z 库；
#: - 根目录只有一层 `mingw64/`，解压时剥掉就是标准的 `bin/ include/ lib/` 布局；
#: - `bin/` 下同时有 gcc.exe / gdb.exe，还带了 libpython3.9.dll 和 lib/python3.9/
#:   —— gdb 的 Python 支持就靠它们，正好满足 cdbg 探针的要求；
#: - **gdb 特意停在 17.2**：winlibs 更新的 r3 版本已经升到 gdb 18.1，它改了几处
#:   `maintenance info sections` / `info proc mappings` 的输出格式，cdbg 现在
#:   的解析器认不出来 —— 表现是「段布局」和「内存区域分区」两块空着。装一套
#:   「能调试但面板一半是空的」工具链，比不装还糟，所以版本号写死。
TOOLCHAIN_PACKAGE = {
    "名称": "MinGW-w64 · winlibs GCC 16.1.0 + GDB 17.2（带 Python）",
    "版本": "16.1.0-r4",
    "url": ("https://github.com/brechtsanders/winlibs_mingw/releases/download/"
            "16.1.0posix-14.0.0-ucrt-r4/"
            "winlibs-x86_64-posix-seh-gcc-16.1.0-mingw-w64ucrt-14.0.0-r4.zip"),
    "sha256": "c406a22f8cac82559a3a1d96b62ff603f666499fb5ff4784e87b4eb6fa37dede",
    "size": 272877601,
}

#: GitHub 加速节点（gh-proxy.org 的公共节点，前缀式反代：节点前缀 + 原始 GitHub URL）。
#: 只在大陆地区、并且用户点头之后才用 —— 下载流量会经过第三方服务，这一点必须
#: 在界面上写明白，不能悄悄替换下载地址。
GHPROXY_NODES = (
    "https://v4.gh-proxy.org/",
    "https://v6.gh-proxy.org/",
    "https://cdn.gh-proxy.org/",
    "https://axisnow.gh-proxy.org/",
)
GHPROXY_SOURCE = "gh-proxy.org（第三方公共 GitHub 反代，非本项目提供）"

#: 老名字，兼容外部引用
TOOLCHAIN_MIRRORS = GHPROXY_NODES

# ---------------------------------------------------------------- 小工具

def _dw(s: str) -> int:
    """字符串的终端显示宽度（中文算两格）。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _dw(s))


def _uniq(seq):
    seen, out = set(), []
    for x in seq:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def _is_executable(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    if IS_WIN:
        return path.lower().endswith((".exe", ".com", ".bat", ".cmd"))
    # POSIX：不看后缀，看实际可执行位
    return os.access(path, os.X_OK)


def _run(exe: str, args, timeout=8, stdin_data=None, env=None):
    """静默执行外部命令；任何异常都吞掉，返回 stdout/stderr 文本。"""
    kw = dict(
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", timeout=timeout,
    )
    if IS_WIN:
        kw["creationflags"] = NO_WINDOW
    if env is not None:
        kw["env"] = env
    if stdin_data is not None:
        kw["input"] = stdin_data
    else:
        kw["stdin"] = subprocess.DEVNULL
    try:
        r = subprocess.run([exe] + list(args), **kw)
        return r.stdout or "", r.stderr or ""
    except Exception:
        return "", ""


def _gdb_env():
    """跑 gdb 时把 PYTHON* 环境变量摘掉。

    gdb 内嵌的是自己的 Python。用户机器上只要设过 PYTHONHOME / PYTHONPATH
    （装过 Python 的人很常见，各种 IDE 也会设），gdb 就会去初始化外面那套
    解释器，报 `Python initialization failed: failed to get the Python codec`，
    结果就是「调试器能停能走，变量面板全空」。
    """
    env = dict(os.environ)
    for name in [k for k in env if k.upper().startswith("PYTHON")]:
        env.pop(name, None)
    return env


def _strip_json_comments(text: str) -> str:
    """settings.json 允许 // 注释和尾逗号，这里做最小清理。"""
    out, i, n = [], 0, len(text)
    in_str = esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return re.sub(r",\s*([}\]])", r"\1", "".join(out))


def _env(name: str) -> str:
    return os.environ.get(name, "") or ""


def _candidate_dirs_from_roots(roots, patterns):
    """在给定根目录下按相对模式找 bin 目录。"""
    found = {}
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for pat in patterns:
            for p in glob.glob(os.path.join(root, pat)):
                if os.path.isdir(p) and any(
                    _is_executable(os.path.join(p, h)) for h in EXE_HINTS
                ):
                    found[os.path.normcase(os.path.normpath(p))] = p
    return found


# ---------------------------------------------------------------- 数据模型

@dataclass
class Toolchain:
    bindir: str
    name: str
    family: str
    exe: str
    version: str = "未知"
    sources: list = field(default_factory=list)
    tools: dict = field(default_factory=dict)
    include_dirs: list = field(default_factory=list)
    headers: dict = field(default_factory=dict)
    lib_status: str = "未知"
    missing: list = field(default_factory=list)
    gdb_python: str = "未知"
    """该工具链的 gdb 是否带 Python 支持（cdbg 的探针必须靠它）。
    取值：「有」/「无」/「未知」（没有 gdb 时）。"""

    usable: bool = True
    """能不能拿来给 cdbg 用 —— 判据不是「能不能编译」，而是「编译出来的
    程序 GDB 能不能读到 DWARF」。判据见 `_judge_usability`。"""

    unusable_reason: str = ""
    """不可用时的一句话原因，直接展示给学生看。"""

    @property
    def key(self) -> str:
        return os.path.normcase(os.path.normpath(self.bindir))

    def source_text(self) -> str:
        return ", ".join(self.sources)

    def status_text(self) -> str:
        return "可用" if self.usable else "不可用（%s）" % (self.unusable_reason or "原因未知")

    def summary(self) -> str:
        lib = "库完整" if self.lib_status == "完整" else (
            "库基本完整" if self.lib_status == "基本完整" else "库不完整")
        return "%s | %s | %s | %s | %s | %s" % (
            self.name, self.family, self.version, self.exe, lib, self.status_text())


# ---------------------------------------------------------------- 来源搜刮

def scan_path_dirs():
    """从 PATH 环境变量里搜刮。"""
    found = {}
    for d in _env("PATH").split(os.pathsep):
        d = d.strip().strip('"')
        if not d or not os.path.isdir(d):
            continue
        if any(_is_executable(os.path.join(d, h)) for h in EXE_HINTS):
            # /bin 常常只是 /usr/bin 的符号链接；按真实路径去重，
            # 否则扫描结果里会冒出两套一模一样的工具链。
            key = os.path.normcase(os.path.realpath(d))
            found.setdefault(key, d)
    return found


def scan_env_vars():
    """搜刮 PATH 之外的编译器相关环境变量（CC / CXX / CDBG_CC / *_HOME ...）。"""
    import shutil
    found = {}
    names = (
        "CC", "CXX", "CPP", "GCC", "GXX", "CDBG_CC", "CDBG_GDB",
        "MINGW_HOME", "MINGW64_HOME", "MSYS2_HOME", "MSYSTEM_PREFIX",
        "DEVCPP_HOME", "MSVC_HOME",
    )
    for name in names:
        raw = _env(name).strip().strip('"')
        if not raw:
            continue
        p = raw
        if not os.path.isfile(p) and not os.path.isdir(p):
            resolved = shutil.which(raw)
            if resolved:
                p = resolved
        candidates = []
        if os.path.isfile(p):
            candidates.append(os.path.dirname(p))
        elif os.path.isdir(p):
            candidates += [p, os.path.join(p, "bin")]
        for d in candidates:
            if os.path.isdir(d) and any(
                _is_executable(os.path.join(d, h)) for h in EXE_HINTS
            ):
                key = os.path.normcase(os.path.normpath(d))
                if key in found:
                    found[key] = (found[key][0], found[key][1] + ", " + name)
                else:
                    found[key] = (os.path.normpath(d), "环境变量 " + name)
    return found


def scan_devcpp():
    """Dev-C++（含其内嵌的 MinGW32/MinGW64）。仅 Windows。"""
    if not IS_WIN:
        return {}
    roots = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "USERPROFILE"):
        base = _env(var)
        if base:
            roots += [os.path.join(base, "Dev-Cpp"), os.path.join(base, "DevCpp")]
    # 注册表里 Dev-C++ 可能把安装目录记在 Uninstall 项下
    if IS_WIN:
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for sub in (r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Dev-C++",
                            r"Software\Dev-C++"):
                    try:
                        with winreg.OpenKey(hive, sub) as k:
                            for val in ("InstallLocation", "Path", "App Path"):
                                try:
                                    v, _ = winreg.QueryValueEx(k, val)
                                    if v:
                                        roots.append(str(v))
                                except OSError:
                                    pass
                    except OSError:
                        pass
        except Exception:
            pass
    return _candidate_dirs_from_roots(roots, (
        "MinGW*/bin", "mingw*/bin", "bin", "*",  # 最后一层兜底：Dev-Cpp/<某目录>/bin
    ))


def scan_vscode():
    """VS Code：读 settings.json 里的 compilerPath，再扫常见插件目录。"""
    found = {}
    home = _env("USERPROFILE") or os.path.expanduser("~")
    appdata = _env("APPDATA")
    settings = []
    if appdata:
        settings.append(os.path.join(appdata, "Code", "User", "settings.json"))
        settings.append(os.path.join(appdata, "Code - Insiders", "User", "settings.json"))
    settings += [
        os.path.join(home, ".vscode", "settings.json"),
        os.path.join(os.getcwd(), ".vscode", "settings.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".vscode", "settings.json"),
    ]
    keys = ("compilerPath", "cCompilerPath", "cppCompilerPath", "default.compilerPath",
            "C_Cpp.default.compilerPath", "C_Cpp.default.cCompilerPath",
            "C_Cpp.default.cppCompilerPath")
    for s in settings:
        if not os.path.isfile(s):
            continue
        try:
            with open(s, "r", encoding="utf-8-sig", errors="replace") as f:
                raw = f.read()
            data = json.loads(_strip_json_comments(raw))

            def walk(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k in keys and isinstance(v, str) and v and not v.startswith("${"):
                            p = os.path.normpath(os.path.expandvars(v))
                            if os.path.isfile(p):
                                d = os.path.dirname(p)
                                found[os.path.normcase(d)] = d
                        else:
                            walk(v)
                elif isinstance(obj, list):
                    for v in obj:
                        walk(v)
            walk(data)
        except Exception:
            pass
    # VS Code 的 C/C++ 扩展本身不带编译器，但工作区里常有人把 MinGW 塞在插件旁
    for extdir in (os.path.join(home, ".vscode", "extensions"),
                   os.path.join(appdata, "Code", "extensions") if appdata else ""):
        if extdir and os.path.isdir(extdir):
            found.update(_candidate_dirs_from_roots(
                (extdir,), ("*/bin", "*/*/bin", "*/*/*/bin")))
    return found


def scan_visual_studio():
    """Visual Studio：vswhere + 常见 MSVC 工具目录。仅 Windows。"""
    if not IS_WIN:
        return {}
    found = {}
    pf = _env("ProgramFiles")
    pf86 = _env("ProgramFiles(x86)")
    roots = []
    if pf86:
        vswhere = os.path.join(pf86, "Microsoft Visual Studio", "Installer", "vswhere.exe")
        if os.path.isfile(vswhere):
            out, _ = _run(vswhere, ["-latest", "-property", "installationPath"], timeout=6)
            if out.strip():
                roots.append(out.strip())
        roots.append(os.path.join(pf86, "Microsoft Visual Studio"))
    if pf:
        roots.append(os.path.join(pf, "Microsoft Visual Studio"))
    found.update(_candidate_dirs_from_roots(roots, (
        "*/VC/Tools/MSVC/*/bin/Hostx64/x64",
        "*/VC/Tools/MSVC/*/bin/Hostx86/x86",
        "VC/Tools/MSVC/*/bin/Hostx64/x64",
        "18/*/VC/Tools/MSVC/*/bin/Hostx64/x64",
    )))
    return found


def scan_embedded():
    """「内嵌」编译器：模块自身目录、父目录、当前目录下捆绑的工具链。"""
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    roots = [here, parent, os.getcwd()]
    return _candidate_dirs_from_roots(roots, (
        "bin", "tools/bin", "compiler/bin", "compilers/*/bin",
        "MinGW*/bin", "mingw*/bin", "gcc*/bin", "Dev-Cpp*/MinGW*/bin",
        "*/*/bin",
    ))


def scan_common_roots():
    """MSYS2 / MinGW-w64 / TDM / Cygwin / Arduino 等常见安装位置。仅 Windows。

    Linux 上的编译器本来就装在 PATH（/usr/bin、/usr/local/bin）里，
    由 `scan_path_dirs()` 负责；这里不盲目扫全盘。
    """
    if not IS_WIN:
        return {}
    pats = (
        "msys64/*/bin", "msys2/*/bin", "MinGW/bin", "mingw64/bin",
        "mingw-w64/*/bin", "TDM-GCC-*/bin", "cygwin*/bin", "w64devkit/bin",
        "WinLibs/*/mingw64/bin", "Programs/Arduino IDE/hardware/tools/avr/bin",
        "Programs/Arduino IDE/*/hardware/tools/avr/bin",
    )
    roots = []
    drive = os.path.splitdrive(os.path.abspath(__file__))[0] or "C:"
    roots.append(drive + os.sep)
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "USERPROFILE"):
        if _env(var):
            roots.append(_env(var))
    home = _env("USERPROFILE") or os.path.expanduser("~")
    roots.append(os.path.join(home, "AppData", "Local", "Programs"))
    return _candidate_dirs_from_roots(roots, pats)


# ---------------------------------------------------------------- 探测细节

def _detect_family(bindir: str):
    names = set()
    try:
        names = {f.lower() for f in os.listdir(bindir)}
    except Exception:
        pass
    if "cl.exe" in names or "cl" in names:
        return "MSVC"
    if "avr-gcc.exe" in names or "avr-gcc" in names:
        return "GCC (AVR)"
    if names & {"gcc.exe", "gcc", "g++.exe", "g++"}:
        return "GCC"
    if names & {"clang.exe", "clang", "clang++.exe", "clang++"}:
        return "Clang"
    return None


def _detect_exe(bindir: str, family: str) -> str:
    prefer = {
        "GCC": TOOL_CANDIDATES["gcc"] + TOOL_CANDIDATES["g++"],
        "GCC (AVR)": ("avr-gcc.exe", "avr-g++.exe", "avr-gcc"),
        "Clang": ("clang.exe", "clang++.exe", "clang", "clang++"),
        "MSVC": ("cl.exe", "cl"),
    }.get(family, ())
    for n in prefer:
        p = os.path.join(bindir, n)
        if _is_executable(p):
            return p
    return ""


def _guess_name(bindir: str, source: str) -> str:
    if not IS_WIN:
        low = bindir.replace("\\", "/").lower()
        if "clang" in low:
            return "系统 Clang"
        if low in ("/usr/bin", "/bin"):
            return "系统 GCC"
        if "/usr/local/" in low:
            return "本地安装 GCC"
        if "PATH" in source:
            return "PATH 中的 GCC"
        return "系统 GCC"
    p = bindir.replace("/", "\\")
    low = p.lower()
    if "msys64" in low or "msys2" in low:
        m = re.search(r"\\(ucrt64|mingw64|mingw32|clang64|clangarm64|msys|clang32)\\bin$", low)
        return "MSYS2 " + (m.group(1).upper() if m else "工具链")
    if "dev-cpp" in low or "devcpp" in low:
        m = re.search(r"\\(mingw32|mingw64)\\bin$", low)
        return "Dev-C++ 内嵌 " + (m.group(1) if m else "MinGW")
    if "arduino" in low:
        return "Arduino AVR-GCC"
    if "visual studio" in low:
        m = re.search(r"MSVC[\\/](\d+\.\d+\.\d+)", p)
        return "Visual Studio MSVC" + (" " + m.group(1) if m else "")
    if "clang" in low:
        return "Clang"
    if "mingw" in low:
        return "MinGW-w64"
    if "PATH" in source:
        return "PATH 中的 GCC"
    return os.path.basename(os.path.dirname(bindir)) or bindir


def _probe_version(exe: str, family: str) -> str:
    if not exe:
        return "未知"
    if family == "MSVC":
        out, err = _run(exe, [], timeout=8)
        m = re.search(r"Version\s+(\d+\.\d+(?:\.\d+)*)", out + err, re.I)
        if m:
            return m.group(1)
        m = re.search(r"MSVC[\\/](\d+\.\d+\.\d+)", exe)
        return (m.group(1) + "（路径推断）") if m else "未知"
    out, err = _run(exe, ["--version"], timeout=8)
    lines = [x.strip() for x in (out + "\n" + err).splitlines() if x.strip()]
    if not lines:
        return "未知"
    ver = re.compile(r"\b(\d+\.\d+(?:\.\d+)*)\b")
    for line in lines[:6]:
        m = ver.search(line)
        if m:
            return m.group(1)
    return lines[0][:60]


def _probe_gdb_python(gdb_exe: str) -> str:
    """检查 gdb 里能不能跑 Python —— cdbg 的数据采集全靠它。

    `gdb --batch -ex "python print(1)"` 能打出 1 才算合格；裁掉 Python 的
    gdb（有些精简版发行包只留了 gdb 的 C 部分）调试起来会一声不响地不出变量。
    """
    if not gdb_exe or not _is_executable(gdb_exe):
        return "未知"
    out, err = _run(gdb_exe, ["--batch", "--nx", "-ex", "python print(1)"],
                    timeout=10, env=_gdb_env())
    text = (out or "") + (err or "")
    if re.search(r"(^|\n)\s*1\s*(\n|$)", text):
        return "有"
    return "无"


def _judge_usability(family: str, exe: str, tools: dict, gdb_python: str):
    """这套工具链能不能给 cdbg 用？返回 (可用?, 原因)。

    判据是「编译出来的程序，GDB 能不能读到调试信息」，不是「能不能编译」：

    - **MSVC（cl.exe）**：产出 PDB 调试信息，是 Windows 的私有格式，GDB 只认
      DWARF。用它编译出来的 exe 能编译、能启动，但变量、行号、调用栈全是空的
      —— 这不是「少支持几个特性」，是调试信息格式根本对不上。所以它只能是
      占位（让人看见「你机器上只有 MSVC」），永远不自动选中。
    - **AVR-GCC**：交叉编译器，产出的是单片机固件，本机既跑不了也调不了。
    - **没有 gdb**：只能编译，不能调试。
    - **gdb 不带 Python**：`gdb_probe.py` 是把 Python 脚本注入 gdb 去采集变量的，
      没有 Python 支持就是「调试器起来了，面板一片空白」。
    """
    if not family or family == "未知":
        return False, "没认出编译器类型"
    if not exe or not _is_executable(exe):
        return False, "这个目录里没找到编译器主程序"
    if family == "MSVC":
        return False, "MSVC 输出 PDB 调试信息，GDB 只认 DWARF：能编译能跑，但看不到变量和调用栈"
    if family == "GCC (AVR)":
        return False, "AVR 交叉编译器，产出的是单片机程序，本机跑不起来"
    gdb = (tools or {}).get("gdb") or ""
    if not gdb or not _is_executable(gdb):
        return False, "这套工具链里没有 gdb：只能编译，不能调试"
    if gdb_python == "无":
        return False, "gdb 不带 Python 支持，采集不到变量"
    if gdb_python != "有":
        return False, "gdb 探测不到 Python 支持"
    return True, ""


def _probe_include_dirs(exe: str, family: str, bindir: str) -> list:
    dirs = []
    if family.startswith("GCC") or family == "Clang":
        # 让编译器自己说出头文件搜索路径，比猜目录可靠。
        # 先问 C++；只有 C 编译器（没装 g++）时退回 C —— 否则会把一套
        # 能正常调试 C 的工具链误判成「库不完整」。
        def _search_dirs(lang):
            _, err = _run(exe, ["-E", "-x", lang, "-v", "-"], timeout=10,
                          stdin_data="")
            m = re.search(
                r"#include <\.\.\.> search starts here:\s*(.*?)\s*End of search list\.",
                err, re.S)
            if not m:
                return []
            out = []
            for line in m.group(1).splitlines():
                line = line.strip()
                if line and not line.lower().startswith("ignoring") and os.path.isdir(line):
                    out.append(os.path.normpath(line))
            return out

        dirs = _search_dirs("c++") or _search_dirs("c")
    root = os.path.dirname(bindir)
    if not dirs:
        # 老版本 gcc（Dev-C++ 自带那种）可能不吐搜索路径，退回目录推断
        dirs = [
            os.path.join(root, "include"),
            os.path.join(bindir, "..", "include"),
            os.path.join(root, "lib", "gcc"),
        ]
    if family.startswith("GCC") or family == "Clang":
        # MinGW/MSYS2 的 CRT 头文件常在 <root>/<target-triplet>/include，
        # 编译器自报路径有时不含它，补上这一路才算看清「是否带基础库」。
        try:
            for sub in os.listdir(root):
                low = sub.lower()
                if any(k in low for k in ("w64-mingw32", "-mingw32", "mingw64", "ucrt", "clang")):
                    p = os.path.join(root, sub, "include")
                    if os.path.isdir(p):
                        dirs.append(p)
        except Exception:
            pass
        # C++ 标准库：include/c++/<版本>/ 及其平台子目录
        cpp = os.path.join(root, "include", "c++")
        if os.path.isdir(cpp):
            for ver in os.listdir(cpp):
                vd = os.path.join(cpp, ver)
                if os.path.isdir(vd):
                    dirs.append(vd)
                    for sub in os.listdir(vd):
                        p = os.path.join(vd, sub)
                        if os.path.isdir(p):
                            dirs.append(p)
    if family == "MSVC":
        # cl.exe 在 <VS>/VC/Tools/MSVC/<ver>/bin/Hostx64/x64
        m = re.search(r"(.*?VC[\\/]Tools[\\/]MSVC[\\/][^\\/]+)[\\/]bin", bindir)
        if m:
            vc = m.group(1)
            dirs.append(os.path.join(vc, "include"))
            kits = os.path.join(os.path.dirname(os.path.dirname(vc)), "Windows Kits", "10", "Include")
            if os.path.isdir(kits):
                for v in os.listdir(kits):
                    for sub in ("ucrt", "um", "shared"):
                        p = os.path.join(kits, v, sub)
                        if os.path.isdir(p):
                            dirs.append(p)
        dirs = [d for d in dirs if os.path.isdir(d)]
    return _uniq(dirs)


def _check_headers(dirs: list) -> dict:
    return {
        h: any(os.path.isfile(os.path.join(d, h)) for d in dirs)
        for h in HEADERS
    }


def _lib_status(headers: dict):
    missing = [h for h in HEADERS if not headers.get(h)]
    if not missing:
        return "完整", []
    if all(headers.get(h) for h in CORE_HEADERS):
        return "基本完整", missing
    return "不完整", missing


def _collect_tools(bindir: str) -> dict:
    tools = {}
    for key, names in TOOL_CANDIDATES.items():
        for n in names:
            p = os.path.join(bindir, n)
            if _is_executable(p):
                tools[key] = p
                break
        else:
            tools[key] = ""
    return tools


def build_toolchain(bindir: str, sources: set) -> Toolchain:
    bindir = os.path.normpath(bindir)
    family = _detect_family(bindir) or "未知"
    exe = _detect_exe(bindir, family)
    source_list = sorted(sources)
    tc = Toolchain(
        bindir=bindir,
        name=_guess_name(bindir, " ".join(source_list)),
        family=family,
        exe=exe,
        sources=source_list,
        tools=_collect_tools(bindir),
    )
    tc.version = _probe_version(exe, family) if exe else "未知"
    tc.include_dirs = _probe_include_dirs(exe, family, bindir) if exe else []
    tc.headers = _check_headers(tc.include_dirs)
    tc.lib_status, tc.missing = _lib_status(tc.headers)
    tc.gdb_python = _probe_gdb_python(tc.tools.get("gdb", ""))
    tc.usable, tc.unusable_reason = _judge_usability(
        family, exe, tc.tools, tc.gdb_python)
    return tc


def _order(tc: Toolchain):
    """扫描结果的排序键：能调试的排前面。

    第一优先：可用（能编译 + 能调试 + gdb 带 Python）。不可用的（MSVC、
    gdb 没有 Python、没有 gdb）一律沉底 —— 它们留在列表里是为了「说明情况」，
    不是给人选的。
    """
    ok = 0 if tc.usable else 1
    py = 0 if tc.gdb_python == "有" else 1
    prio = 0 if any("PATH" in s for s in tc.sources) else 1
    if any("VS Code" in s for s in tc.sources):
        prio = 0
    libp = 0 if tc.lib_status == "完整" else (1 if tc.lib_status == "基本完整" else 2)
    return (ok, py, prio, libp, tc.name.lower())


def scan_all() -> list:
    """跑完所有来源，合并同一目录（同一套工具链），返回排序后的列表。"""
    merged = {}

    def add(found, label):
        for key, path in found.items():
            merged.setdefault(key, {"path": path, "sources": set()})
            merged[key]["sources"].add(label)

    for key, (path, label) in scan_env_vars().items():
        merged.setdefault(key, {"path": path, "sources": set()})
        merged[key]["sources"].add(label)

    add(scan_path_dirs(), "PATH 环境变量")
    add(scan_devcpp(), "Dev-C++")
    add(scan_vscode(), "VS Code")
    add(scan_visual_studio(), "Visual Studio")
    add(scan_embedded(), "内嵌/捆绑")
    add(scan_common_roots(), "常见安装目录")

    tcs = [build_toolchain(v["path"], v["sources"]) for v in merged.values()]
    tcs.sort(key=_order)
    return tcs


# ---------------------------------------------------------------- 配置文件

def _write_lines(path: str, lines):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def _kv(tc: Toolchain, indent="") -> list:
    """一套工具链的字段块。"""
    yes = lambda h: "有" if tc.headers.get(h) else "缺"
    return [
        indent + "名称 = " + tc.name,
        indent + "编译器类型 = " + tc.family,
        indent + "版本号 = " + tc.version,
        indent + "主程序 = " + tc.exe,
        indent + "来源 = " + tc.source_text(),
        indent + "是否携带基础运行库 = " + tc.lib_status,
        indent + "是否可用于调试 = " + ("可用" if tc.usable else "不可用"),
        indent + "不可用原因 = " + (tc.unusable_reason or "无"),
        indent + "缺失头文件 = " + (", ".join(tc.missing) if tc.missing else "无"),
        indent + "头文件检查 = " + ", ".join(
            "%s:%s" % (h, yes(h)) for h in HEADERS),
        indent + "include搜索路径 = " + (" | ".join(tc.include_dirs) if tc.include_dirs else "无"),
        indent + "gcc = " + tc.tools.get("gcc", ""),
        indent + "g++ = " + tc.tools.get("g++", ""),
        indent + "make = " + tc.tools.get("make", ""),
        indent + "gdb = " + tc.tools.get("gdb", ""),
        indent + "gdb的Python支持 = " + tc.gdb_python,
        indent + "nm = " + tc.tools.get("nm", ""),
        indent + "windres = " + tc.tools.get("windres", ""),
        indent + "gprof = " + tc.tools.get("gprof", ""),
    ]


def write_config(path: str, tcs: list, selected_key: str, scan_time: str):
    sel = next((t for t in tcs if t.key == selected_key), tcs[0] if tcs else None)
    L = [
        "# ============================================================",
        "# 编译器搜刮模块 · 配置文件",
        "# 由 compiler_scanner.py 生成，每次扫描会重写本文件",
        "# 设置项1 = 扫描结果；设置项2 = 你选中的编译器（TUI 写入）",
        "# ============================================================",
        "",
        "[概况]",
        "扫描时间 = " + scan_time,
        "编译器数量 = %d" % len(tcs),
        "生成器 = compiler_scanner.py",
        "",
        "[设置项1-扫描到的编译器]",
        "数量 = %d" % len(tcs),
    ]
    for i, tc in enumerate(tcs, 1):
        L.append("编译器%02d = %s" % (i, tc.summary()))
    L.append("")
    for i, tc in enumerate(tcs, 1):
        L.append("[编译器%02d]" % i)
        L += _kv(tc)
        L.append("")
    L.append("[设置项2-当前编译器]")
    if sel:
        L.append("选中序号 = %02d" % (tcs.index(sel) + 1))
        L += _kv(sel)
    else:
        L.append("选中序号 = 0")
        L.append("说明 = 未找到任何编译器")
    _write_lines(path, L)


def read_selection(path: str) -> str:
    """读回上次选中的主程序路径，用于再次扫描时保留选择。"""
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        in_sec = False
        for line in lines:
            s = line.strip()
            if s.startswith("["):
                in_sec = s.startswith("[设置项2")
                continue
            if in_sec and s.startswith("主程序 ="):
                return s.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- 补装工具链
#
# 什么时候会走到这里
# ------------------
# `_judge_usability` 一套可用的都判不出来时（机器上只有 MSVC、gdb 不带
# Python、或者干脆没装编译器），cdbg 不是硬着头皮去编译一个「看不到变量」
# 的 exe，而是问一句要不要装一套 MinGW-w64。装完仍然用 `write_config` 把
# 路径写进 `config/toolchain.conf`，下次启动直接复用。
#
# 约束
# ----
# - 纯标准库：下载 urllib、解压 zipfile、改用户环境变量 winreg。
# - 下载要能断点续传、能换镜像：国内直连 GitHub Releases 常常几十 KB/s。
# - 必须校验 sha256：不然装进去的可能是半截文件，或者被中间人换过的东西。
# - 写环境变量只写 **用户** Path（HKCU\Environment），不需要管理员。

USER_AGENT = "cdbg-compiler-scanner/1.0"


def _install_supported() -> bool:
    """能不能走「自动补装」。

    补装包是 Windows 的 MinGW-w64（zip 里是 .exe），Linux 上缺编译器应该
    交给包管理器（`sudo apt install gcc gdb`），不该塞一份 Windows 二进制进去。
    """
    return IS_WIN


def _out(msg: str = "") -> None:
    try:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _stdout_is_console() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _ask_yes(prompt: str, default: bool = False) -> bool:
    """命令行 yes/no。读不到输入（管道、EOF、Ctrl-C）一律按默认值走。"""
    if not _stdin_is_console():
        return default
    tail = "[Y/n]" if default else "[y/N]"
    try:
        ans = input("  %s %s " % (prompt, tail)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        _out("")
        return default
    if not ans:
        return default
    return ans[0] in ("y", "1", "是", "好", "a")


def _has_wide_chars(path: str) -> bool:
    """路径里有没有非 ASCII 字符（中文用户名 / 中文目录）。

    不是嫌弃中文：MinGW 的 gcc 在含非 ASCII 的路径下经常读不到头文件，
    报出来的错还很难懂，所以在能选的时候先把这种位置拦下来。
    """
    try:
        return any(ord(ch) > 127 for ch in str(path))
    except Exception:
        return False


def _in_temp_dir(path: str) -> bool:
    """这个路径是不是落在临时目录里（临时目录里的东西随时会被系统清掉）。"""
    try:
        low = os.path.normcase(os.path.abspath(path))
    except Exception:
        return False
    for var in ("TEMP", "TMP", "TMPDIR"):
        t = _env(var)
        if not t:
            continue
        t = os.path.normcase(os.path.abspath(t))
        if low == t or low.startswith(t.rstrip("\\/") + os.sep):
            return True
    return False


def _can_write(directory: str) -> bool:
    """这个位置能不能写。

    只试写、**不建目录**：找最近的已存在祖先写一个探针文件就够判断了，
    免得光是把三个候选项列出来就在程序目录里留下一个空的 Mingw64。
    """
    probe_dir = os.path.abspath(directory)
    while probe_dir and not os.path.isdir(probe_dir):
        parent = os.path.dirname(probe_dir)
        if parent == probe_dir:
            return False
        probe_dir = parent
    probe = os.path.join(probe_dir, ".cdbg-write-test")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("x")
        return True
    except OSError:
        return False
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def _local_appdata() -> str:
    base = _env("LOCALAPPDATA")
    if base:
        return base
    home = _env("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, "AppData", "Local")


def _is_mainland_china() -> bool:
    """设备地区是不是中国大陆。

    不联网查 IP：启动路径上不该为了判断地区去连第三方，而且慢。
    只用本机信号，任一命中就算：
    - Windows「区域」里选的国家代码（GeoNation，45 = 中国）；
    - 系统默认区域名 zh-CN / zh-Hans；
    - 时区名 China Standard Time / 中国标准时间。
    """
    if IS_WIN:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Control Panel\International\Geo") as key:
                nation, _ = winreg.QueryValueEx(key, "Nation")
                if str(nation).strip() == "45":
                    return True
        except Exception:
            pass
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(96)
            n = ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 96)
            name = (buf.value if n else "").lower().replace("_", "-")
            if name.startswith("zh-cn") or (name.startswith("zh-hans") and "cn" in name):
                return True
        except Exception:
            pass
    tz = " ".join(time.tzname or ()).lower()
    if "china standard time" in tz or "中国标准时间" in tz:
        return True
    env_lang = (_env("LANG") or _env("LC_ALL")).lower()
    return env_lang.startswith("zh_cn") or env_lang.startswith("zh-cn")


def _acceleration_allowed_by_env() -> bool:
    return _env("CDBG_NO_ACCELERATE").strip().lower() not in ("1", "true", "yes", "on")


def choose_acceleration(interactive: bool = True):
    """决定这次下载走不走加速。返回 (要不要加速, 说明文字)。

    - 系统不在中国大陆：直连，不问（没必要多一句话）。
    - 在大陆、能提问：把服务来源和节点列出来，问一句；默认建议开。
    - 在大陆、不能提问（`--install` / 管道）：默认开，但在输出里写明走了谁；
      想强制直连就设 `CDBG_NO_ACCELERATE=1`。
    """
    if not _acceleration_allowed_by_env():
        return False, "已设置 CDBG_NO_ACCELERATE，直连 GitHub"
    if not _is_mainland_china():
        return False, "系统区域不在中国大陆，直连 GitHub"
    if not interactive:
        return True, "系统区域在中国大陆，已自动启用 %s（可用 CDBG_NO_ACCELERATE=1 关掉）" % GHPROXY_SOURCE
    _out("  检测到系统区域：中国大陆，直连 GitHub Releases 常常只有几十 KB/s。")
    _out("  可加速下载，节点来自 %s：" % GHPROXY_SOURCE)
    for node in GHPROXY_NODES:
        _out("      %s" % node.rstrip("/"))
    _out("  启用后下载请求（含文件内容）会经过上面这个第三方服务。")
    if _ask_yes("启用 GitHub 加速？", default=True):
        return True, "已启用 %s" % GHPROXY_SOURCE
    return False, "按你的选择直连 GitHub"


def _sample_speed(url: str, timeout: float = 4.0, sample: int = 131072) -> float:
    """取一小段（默认 128KB）估算速度，返回字节/秒；失败返回 0。"""
    import urllib.request

    t0 = time.time()
    try:
        req = urllib.request.Request(
            url, headers={"Range": "bytes=0-%d" % (sample - 1), "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(sample)
        dt = max(time.time() - t0, 0.001)
        return len(data) / dt
    except Exception:
        return 0.0


def rank_mirrors(pkg_url: str, nodes=None, timeout: float = 4.0) -> list:
    """并行给每个节点测个速，返回 [(节点, 字节/秒)]，快的在前；连不上的不返回。"""
    import threading

    if nodes is None:                 # 别把默认值写死在签名里，方便运行期替换节点表
        nodes = GHPROXY_NODES
    scores = {}
    lock = threading.Lock()

    def work(node):
        speed = _sample_speed(node + pkg_url, timeout)
        with lock:
            scores[node] = max(scores.get(node, 0.0), speed)

    threads = [threading.Thread(target=work, args=(n,), daemon=True) for n in nodes]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 2)
    ranked = [(n, s) for n, s in scores.items() if s > 0]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


def _install_targets():
    """三个候选安装位置。

    1. 程序目录旁的 `Mingw64/` —— 跟着程序走，解压到 U 盘也能用；
    2. `%LOCALAPPDATA%\\cdbg\\toolchain\\` —— 不用管理员，最稳，默认选它；
    3. `C:\\Mingw\\` —— 全机共用，得先确认写权限。

    返回 [(序号, 绝对路径, 说明, 可选?, 不可选的原因)]。
    """
    sysdrive = _env("SystemDrive") or (os.path.splitdrive(os.path.abspath(PROJECT_ROOT))[0] or "C:")
    portable = os.path.join(PROJECT_ROOT, "Mingw64")
    user_dir = os.path.join(_local_appdata(), "cdbg", "toolchain")
    machine = os.path.join(sysdrive + os.sep, "Mingw")

    items = []

    # 1) 程序目录下的 .\Mingw64：跟着程序走。
    #    能写的前提是「程序不是从压缩包里临时解压出来跑的」（临时目录里的东西
    #    随时被清），并且路径里没有中文。
    if _in_temp_dir(PROJECT_ROOT):
        items.append((1, portable, "程序目录下（跟着程序走）", False,
                      "程序现在跑在临时目录里，装在这里下次就被清掉了"))
    elif _has_wide_chars(PROJECT_ROOT):
        items.append((1, portable, "程序目录下（跟着程序走）", False,
                      "程序路径含中文，MinGW 在中文路径下会读不到头文件；"
                      "把 cdbg 挪到纯英文目录，或改用 2 / 3"))
    else:
        items.append((1, portable, "程序目录下（跟着程序走）",
                      _can_write(portable), "这个目录写不进去"))

    # 2) %LOCALAPPDATA%\cdbg\toolchain：不需要管理员，最稳。
    ok2 = _can_write(user_dir)
    items.append((2, user_dir, "用户目录（推荐，不需要管理员）", ok2, "这个目录写不进去"))

    # 3) C:\Mingw：全机共用，需要用户点头。
    ok3 = _can_write(machine)
    items.append((3, machine, "C:\\Mingw（全机共用）", ok3,
                  "需要管理员权限才能写 C 盘根目录"))

    return items


def _choose_install_dir():
    """让用户从三个位置里挑一个。返回 (路径, 类别) 或 ("", "")。

    类别只有 "portable" / "user" / "machine" 三种，用来决定「要不要顺手加 PATH」。
    """
    items = _install_targets()
    kind_of = {1: "portable", 2: "user", 3: "machine"}
    _out("")
    _out("  装到哪里？")
    for idx, path, note, ok, why in items:
        if ok:
            _out("    %d) %-48s %s" % (idx, path, note))
        else:
            _out("    %d) %-48s 不可选：%s" % (idx, path, why))
    # 默认选 2（用户目录），它不可写时退回第一个可选的
    default = 2 if items[1][3] else next((i for i, p, n, ok, w in items if ok), 0)
    if not default:
        _out("  三个位置都写不进去，装不了。请用管理员身份运行，或先腾出权限。")
        return "", ""

    tail = "（直接回车 = %d）" % default
    for _ in range(6):
        try:
            ans = input("  选择 1/2/3 %s: " % tail).strip()
        except (EOFError, KeyboardInterrupt):
            _out("")
            return "", ""
        if not ans:
            ans = str(default)
        if ans not in ("1", "2", "3"):
            _out("  只能输 1、2 或 3。")
            continue
        idx = int(ans)
        path, ok = items[idx - 1][1], items[idx - 1][3]
        if not ok:
            _out("  %d 现在不可选：%s" % (idx, items[idx - 1][4]))
            continue
        # 3) C:\Mingw 要用户明确点头
        if idx == 3 and not _ask_yes("确认往 C:\\ 根目录下建 %s ？" % path, default=False):
            continue
        return path, kind_of[idx]
    return "", ""


def _fmt_mb(n) -> str:
    return "%.1f MB" % (n / 1048576.0) if n else "? MB"


def _progress(done: int, total: int, elapsed: float, final: bool = False) -> None:
    if not _stdout_is_console():
        return
    speed = (done / elapsed / 1048576.0) if elapsed > 0.05 else 0.0
    pct = ("%5.1f%%" % (done * 100.0 / total)) if total else "  ?  "
    sys.stdout.write("\r    %s  %s / %s  %.2f MB/s   " % (
        pct, _fmt_mb(done), _fmt_mb(total), speed))
    if final:
        sys.stdout.write("\n")
    sys.stdout.flush()


def _sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _download_once(url: str, dest: str, expect_size: int) -> tuple:
    """一次下载尝试，带断点续传。返回 (成功?, 错误说明)。"""
    import urllib.error
    import urllib.request

    got = os.path.getsize(dest) if os.path.isfile(dest) else 0
    if expect_size and got > expect_size:      # 上次下坏了，重来
        got = 0
        try:
            os.remove(dest)
        except OSError:
            pass
    if expect_size and got == expect_size:
        return True, ""

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if got:
        req.add_header("Range", "bytes=%d-" % got)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and got:            # 服务端说已经下完了
            return True, ""
        return False, "HTTP %d" % exc.code
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)

    # 服务端不认 Range（返回 200 而不是 206）就只能从头写
    resumed = got > 0 and getattr(resp, "status", 200) == 206
    mode = "ab" if resumed else "wb"
    if not resumed:
        got = 0
    try:
        total = expect_size or (int(resp.headers.get("Content-Length") or 0) + got)
        t0 = time.time()
        last = 0.0
        with open(dest, mode) as fh:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last > 0.4:
                    last = now
                    _progress(got, total, now - t0)
        _progress(got, total, time.time() - t0, final=True)
    except Exception as exc:
        _progress(got, expect_size or got, 1, final=True)
        return False, "%s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            resp.close()
        except Exception:
            pass

    if expect_size and got < expect_size:
        return False, "只下到 %d / %d 字节（可重跑继续下载）" % (got, expect_size)
    return True, ""


def _download_package(dest: str, rounds: int = 3, mirrors=()) -> tuple:
    """按节点顺序下载，失败换节点、再失败整轮重试。返回 (成功?, 错误说明)。

    `mirrors` 是前缀式加速节点（空 = 直连 GitHub）；顺序由调用方按测速结果排好。
    """
    pkg = TOOLCHAIN_PACKAGE
    urls = []
    custom = _env("CDBG_TOOLCHAIN_URL").strip()      # 内网镜像 / 测试用
    if custom:
        urls.append(custom)
    urls += [m + pkg["url"] for m in mirrors]
    urls.append(pkg["url"])                          # 最后兜底：直连
    urls = _uniq(urls)

    last_err = "没有可用的下载地址"
    for rnd in range(1, rounds + 1):
        for url in urls:
            host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
            _out("  下载 %s（第 %d 轮）…" % (host, rnd))
            ok, err = _download_once(url, dest, int(pkg.get("size") or 0))
            if ok:
                want = pkg.get("sha256") or ""
                if not want:
                    return True, ""
                _out("  校验 sha256 …")
                actual = _sha256_file(dest)
                if actual.lower() == want.lower():
                    _out("  sha256 对上了")
                    return True, ""
                last_err = "sha256 不匹配（得到 %s…）" % actual[:12]
                _out("  %s，删掉重下" % last_err)
                try:
                    os.remove(dest)
                except OSError:
                    pass
                continue
            last_err = err
            _out("  这个源不行：%s" % err)
        if rnd < rounds:
            _out("  全部源都失败，等 3 秒再来一轮（已下载的部分会接着下）…")
            time.sleep(3)
    return False, last_err


def _zip_common_root(zf) -> str:
    """压缩包里如果所有文件都在同一层目录下，返回那层目录名（否则 ""）。"""
    first = None
    for info in zf.infolist():
        name = info.filename.replace("\\", "/").lstrip("/")
        if not name:
            continue
        head = name.split("/")[0]
        if first is None:
            first = head
        elif head != first:
            return ""
    return first or ""


def _zip_strip_prefix(zf) -> str:
    """要剥掉的那层前缀：`bin/gcc.exe` 上面有几层目录，就剥几层。

    比「所有文件共有的顶层目录」稳一点 —— 压缩包里多一个 README 时那个判法
    就失效了，而「gcc 在 bin/ 下」这个事实对任何 MinGW 包都成立。
    """
    for info in zf.infolist():
        parts = info.filename.replace("\\", "/").lstrip("/").split("/")
        for i, part in enumerate(parts[:-1]):
            if part.lower() == "bin" and parts[i + 1].lower() in ("gcc.exe", "g++.exe"):
                return "/".join(parts[:i])
    return _zip_common_root(zf)


def _extract_zip(zip_path: str, target: str) -> int:
    """解压到 target，剥掉压缩包自带的顶层目录。返回写出的文件数。"""
    target_abs = os.path.abspath(target)
    written = 0
    with zipfile.ZipFile(zip_path) as zf:
        root = _zip_strip_prefix(zf)
        entries = zf.infolist()
        for n, info in enumerate(entries, 1):
            name = info.filename.replace("\\", "/")
            if root and name.startswith(root + "/"):
                name = name[len(root) + 1:]
            name = name.lstrip("/")
            if not name:
                continue
            dest = os.path.abspath(os.path.join(target_abs, *name.split("/")))
            if not (dest == target_abs or dest.startswith(target_abs + os.sep)):
                continue                      # zip slip，直接跳过
            if info.is_dir():
                os.makedirs(dest, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            written += 1
            if _stdout_is_console() and n % 500 == 0:
                sys.stdout.write("\r    解压 %d / %d 个文件   " % (n, len(entries)))
                sys.stdout.flush()
        if _stdout_is_console():
            sys.stdout.write("\r    解压完成：%d 个文件            \n" % written)
            sys.stdout.flush()
    return written


def _broadcast_env_change() -> None:
    """通知资源管理器环境变量变了（新开的终端能立刻用上 PATH）。"""
    if not IS_WIN:
        return
    try:
        import ctypes

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 2000, ctypes.byref(ctypes.c_ulong()))
    except Exception:
        pass


def add_to_user_path(bindir: str) -> str:
    """把目录写进「用户环境变量 Path」（HKCU\\Environment，不需要管理员）。

    只改用户变量、不碰系统变量：不用 UAC，也只影响当前用户，出问题好收拾。
    返回 "added" / "already" / "failed: ..."。
    """
    if not IS_WIN:
        return "failed: 只有 Windows 才需要写 PATH"
    bindir = os.path.abspath(bindir).rstrip("\\")
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                current, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, kind = "", winreg.REG_EXPAND_SZ
            parts = [p for p in str(current).split(";") if p.strip()]
            key_norm = os.path.normcase(bindir)
            for p in parts:
                if os.path.normcase(p.strip().strip('"').rstrip("\\")) == key_norm:
                    return "already"
            parts.append(bindir)
            if kind not in (winreg.REG_EXPAND_SZ, winreg.REG_SZ):
                kind = winreg.REG_EXPAND_SZ
            winreg.SetValueEx(key, "Path", 0, kind, ";".join(parts))
    except Exception as exc:
        return "failed: %s" % exc
    # 当前进程也要能立刻看到，不然重扫时 PATH 里还是没有它
    os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + bindir
    _broadcast_env_change()
    return "added"


def _verify_install(bindir: str) -> tuple:
    """装完自检：gcc / gdb 在不在，gdb 带不带 Python。"""
    gcc = os.path.join(bindir, "gcc.exe")
    gdb = os.path.join(bindir, "gdb.exe")
    if not os.path.isfile(gcc):
        return False, "解压完没找到 %s" % gcc
    if not os.path.isfile(gdb):
        return False, "解压完没找到 %s" % gdb
    py = _probe_gdb_python(gdb)
    if py != "有":
        return False, "这套 gdb 不带 Python 支持（探测结果：%s），换一份发行包" % py
    return True, ""


def _rescan_with(bindir: str) -> list:
    """重扫一遍，保证刚装的目录一定在结果里（PATH 还没生效时也认）。"""
    tcs = scan_all()
    key = os.path.normcase(os.path.normpath(bindir))
    if not any(t.key == key for t in tcs):
        tcs.append(build_toolchain(bindir, {"补装"}))
        tcs.sort(key=_order)
    return tcs


def install_toolchain(interactive: bool = True, target: str = "",
                      quiet: bool = False) -> str:
    """补装一套 MinGW-w64。成功返回 bin 目录，失败/用户取消返回 ""。"""
    if not _install_supported():
        _out("  自动补装只支持 Windows（补装包是 MinGW-w64 的 exe）。")
        _out("  Linux 上请用包管理器：sudo apt install gcc gdb")
        return ""
    pkg = TOOLCHAIN_PACKAGE
    size_mb = (pkg.get("size") or 0) / 1048576.0

    if interactive:
        _out("")
        _out("  补装一套能调试的编译器：%s" % pkg["名称"])
        _out("  下载 %.0f MB，来自 GitHub Releases，支持断点续传。" % size_mb)
        if not _ask_yes("现在安装？", default=True):
            _out("  已跳过。以后想装：python cdbg.py --scan-compiler --install")
            return ""
        picked, kind = _choose_install_dir()
        if not picked:
            return ""
        target = picked
    else:
        if not target:
            idx = _install_targets()
            target = next((p for i, p, n, ok, w in idx if ok and i == 2), "")
            if not target:
                target = next((p for i, p, n, ok, w in idx if ok), "")
            kind = "user"
        else:
            kind = "user"

    target = os.path.abspath(target)
    bindir = os.path.join(target, "bin")
    zip_path = target + ".zip"          # 放在目标旁边，重跑能接着下

    if not _can_write(target):
        _out("  写不进去：%s" % target)
        return ""
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        _out("  建目录失败：%s" % exc)
        return ""

    _out("  安装目录：%s" % target)

    # 加速：只在大陆、并且用户同意时才用（详见 choose_acceleration）
    nodes = ()
    use_accel, why = choose_acceleration(interactive)
    _out("  %s" % why)
    if use_accel:
        _out("  正在测各节点速度…")
        ranked = rank_mirrors(pkg["url"])
        if ranked:
            for node, speed in ranked:
                _out("      %-34s %.2f MB/s" % (node.rstrip("/"), speed / 1048576.0))
            _out("  使用节点：%s（%s）" % (ranked[0][0].rstrip("/"), GHPROXY_SOURCE))
            nodes = tuple(n for n, _ in ranked)
        else:
            _out("  加速节点都没连上，改用直连 GitHub。")
            nodes = ()

    start = time.time()
    ok, err = _download_package(zip_path, mirrors=nodes)
    if not ok:
        _out("  下载失败：%s" % err)
        _out("  半个包留在 %s，重跑会接着下。" % zip_path)
        return ""

    _out("  解压到 %s …" % target)
    try:
        _extract_zip(zip_path, target)
    except Exception as exc:
        _out("  解压失败：%s: %s" % (type(exc).__name__, exc))
        return ""
    try:
        os.remove(zip_path)
    except OSError:
        pass

    ok, why = _verify_install(bindir)
    if not ok:
        _out("  自检没通过：%s" % why)
        return ""

    # 环境变量：用户 Path，只影响当前用户、不用管理员。
    want_path = True
    if interactive:
        want_path = _ask_yes(
            "把 %s 加进用户环境变量 Path？（不需要管理员）" % bindir,
            default=(kind == "user"))
    if want_path:
        state = add_to_user_path(bindir)
        if state == "added":
            _out("  已加入用户 Path（新开的终端生效，cdbg 自己已经用上了）")
        elif state == "already":
            _out("  用户 Path 里本来就有这个目录")
        else:
            _out("  写 Path 失败（不影响 cdbg 使用）：%s" % state)

    if not quiet:
        _out("  装好了：%s（用时 %.1fs）" % (bindir, time.time() - start))
    return bindir


def ensure_toolchain(tcs=None, interactive=None, target: str = "",
                     config_path: str = "", force: bool = False) -> bool:
    """没有可用工具链时走补装流程；返回是否装成。

    只在「一套能调试的都没有」时才动作 —— 机器上已经有 gcc + 带 Python 的
    gdb 时永远不会弹这个。装完顺手把结果写进配置，下次启动直接复用。

    没有终端可问（GUI 双击启动、CI）时默认**不**自动下载几百 MB 的工具链，
    只打印提示；真的要装就显式 `--install`（走 force=True）。
    """
    if tcs is None:
        tcs = scan_all()
    if any(tc.usable for tc in tcs):
        return True
    if not _install_supported():
        _out("[工具链] 自动补装只支持 Windows；Linux 请用包管理器装 gcc / gdb。")
        return False
    if interactive is None:
        interactive = _stdin_is_console()
    if not interactive and not force and not target:
        _out("[工具链] 没有终端可以询问，跳过自动补装。")
        _out("         要装：python cdbg.py --scan-compiler --install")
        return False

    _out("[工具链] 没有一套「能编译 + 能调试」的工具链：")
    if tcs:
        for tc in tcs:
            _out("          · %s（%s）—— %s" % (
                tc.name, " ".join(tc.sources) or "?", tc.unusable_reason or "不可用"))
    else:
        _out("          · 本机没扫到任何 C 编译器")

    bindir = install_toolchain(interactive=interactive, target=target)
    if not bindir:
        _out("[工具链] 没有补装。可以自己装 MSYS2 UCRT64 / MinGW-w64，"
             "或设置 CDBG_CC 指向 gcc.exe。")
        return False

    fresh = _rescan_with(bindir)
    tc = next((t for t in fresh if t.key == os.path.normcase(os.path.normpath(bindir))), None)
    if tc is None:
        return False
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        write_config(config_path or DEFAULT_CONFIG_PATH, fresh, tc.key, stamp)
        _out("[工具链] 已选用：%s · gcc %s · gdb Python %s" % (
            tc.name, tc.version, tc.gdb_python))
        _out("         配置 = %s" % (config_path or DEFAULT_CONFIG_PATH))
    except Exception as exc:
        _out("[工具链] 写配置失败（不影响本次使用）：%s" % exc)
    return True


# ---------------------------------------------------------------- TUI

def _stdin_is_console() -> bool:
    """判断 stdin 是不是「真的能读按键」的控制台。

    只看 `sys.stdin.isatty()` 不够：在 Git Bash 里把输入重定向到 `/dev/null`
    时它仍然返回 True，而 msvcrt.getwch() 会永远阻塞 —— 表现就是
    `--scan-compiler` 卡死、Ctrl+C 都退不出来。
    Windows 上再问一次 GetConsoleMode：句柄是 NUL / 管道 / 文件时它会失败。
    """
    try:
        if not sys.stdin.isatty():
            return False
    except Exception:
        return False
    if not IS_WIN:
        return True
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint()
        return bool(k32.GetConsoleMode(handle, ctypes.byref(mode)))
    except Exception:
        return False


def _pkg_short_name() -> str:
    """补装包在列表里显示的短名字（`MinGW-w64 · winlibs GCC ...` → `MinGW-w64`）。"""
    name = str(TOOLCHAIN_PACKAGE.get("名称") or "MinGW-w64")
    for sep in ("·", "（"):
        name = name.split(sep)[0]
    return name.strip() or "MinGW-w64"


class Tui:
    CLEAR = "\033[2J\033[H"
    HIDE = "\033[?25l"
    SHOW = "\033[?25h"
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    REVERSE = "\033[7m"

    def __init__(self, tcs, initial=0, config_path="config"):
        self.tcs = tcs
        # 列表最后一行（Windows 上）是「补装」占位行，所以下标可以等于 len(tcs)
        self.i = max(0, min(initial, self.row_count() - 1))
        self.config_path = config_path
        self.detail = True
        self.msg = ""
        self.result = None

    # ---- 列表 = 真实工具链 + 最后一行「需要下载」占位 ----

    def row_count(self) -> int:
        return len(self.tcs) + (1 if _install_supported() else 0)

    def on_install_row(self, n=None) -> bool:
        if not _install_supported():
            return False
        return (self.i if n is None else n) >= len(self.tcs)

    def install_row_label(self) -> str:
        return "【需要下载】" + _pkg_short_name()


    def _key(self):
        if IS_WIN:
            import msvcrt
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                code = msvcrt.getwch()
                return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}.get(code, "")
            if ch == "\x03":
                raise KeyboardInterrupt
            return ch
        import termios, tty, select
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                if select.select([sys.stdin], [], [], 0.05)[0]:
                    seq = sys.stdin.read(2)
                    return {"[A": "UP", "[B": "DOWN"}.get(seq, "")
                return "ESC"
            if ch == "\x03":
                raise KeyboardInterrupt
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def render(self):
        out = [self.CLEAR + self.HIDE]
        out.append(self.BOLD + "  GCC/G++ 编译器搜刮器" + self.RESET +
                   self.DIM + "  ·  共 %d 套工具链" % len(self.tcs) + self.RESET)
        out.append(self.DIM + "  " + "─" * 66 + self.RESET)
        hint = "↑↓ 选择   Enter 确认   d 详情   "
        hint += "i 补装   " if _install_supported() else ""
        hint += "q 退出"
        out.append("  " + self.DIM + hint + self.RESET)
        out.append("")
        if not self.tcs:
            tail = "按 Enter 补装一套，或 q 退出" if _install_supported() else \
                "先用包管理器装上 gcc / gdb，或 q 退出"
            out.append("  " + self.YELLOW + "没搜到任何编译器。" + self.RESET +
                       self.DIM + "  " + tail + self.RESET)
            out.append("")

        # 真实工具链
        for n, tc in enumerate(self.tcs):
            mark = "▸" if n == self.i else " "
            color = self.GREEN if tc.lib_status == "完整" else (
                self.YELLOW if tc.lib_status == "基本完整" else self.RED)
            lib = "库完整" if tc.lib_status == "完整" else (
                "库基本完整" if tc.lib_status == "基本完整" else "库不全")
            if tc.usable:
                state = self.GREEN + "可用" + self.RESET
            else:
                state = self.RED + "不可用" + self.RESET
            row = "  %s %s  %s  %s  %s  %s  %s" % (
                mark, _pad("%02d" % (n + 1), 2),
                _pad(tc.name, 24), _pad(tc.family, 11),
                _pad(tc.version, 10), color + lib + self.RESET, state)
            out.append(self.REVERSE + row + self.RESET if n == self.i else row)

        # 最后一行：补装占位（选中按 Enter 就装，不用记 i 键）
        if _install_supported():
            n = len(self.tcs)
            mark = "▸" if n == self.i else " "
            row = "  %s %s  %s  %s" % (
                mark, _pad("%02d" % (n + 1), 2), _pad(self.install_row_label(), 24),
                self.DIM + "回车补装" + self.RESET)
            out.append(self.REVERSE + row + self.RESET if n == self.i else row)

        if self.detail:
            out.append("")
            out.append(self.DIM + "  ── 详情 ──────────────────────────────────────────" + self.RESET)
            if self.on_install_row():
                pkg = TOOLCHAIN_PACKAGE
                out.append("  按 Enter 补装 " + _pkg_short_name())
                out.append("  包      " + str(pkg.get("名称") or ""))
                out.append("  体积    约 %.0f MB（GitHub Releases，支持断点续传）" %
                           ((pkg.get("size") or 0) / 1048576.0))
                out.append("  装完    写进 config/toolchain.conf，下次启动直接复用")
                out.append("  下载    直连 GitHub；大陆地区会先问是否走加速（%s）" % GHPROXY_SOURCE)
                out.append("  " + self.DIM + "也可以随时按 i 触发，效果一样" + self.RESET)
            else:
                tc = self.tcs[self.i]
                out.append("  主程序   " + tc.exe)
                out.append("  来源     " + tc.source_text())
                out.append("  include  " + ((" | ".join(tc.include_dirs[:2]))[:76] if tc.include_dirs else "无"))
                for k in ("gcc", "g++", "make", "gdb", "nm", "windres", "gprof"):
                    out.append("  %-7s %s" % (k, tc.tools.get(k) or "—"))
                miss = ", ".join(tc.missing[:6]) or "无"
                out.append("  头文件   " + self.GREEN + "核心齐备" + self.RESET +
                           self.DIM + "  缺: " + miss + self.RESET)
                py_color = self.GREEN if tc.gdb_python == "有" else self.RED
                out.append("  gdb Python " + py_color + tc.gdb_python + self.RESET +
                           self.DIM + "  （cdbg 采集变量必须靠它）" + self.RESET)
                if tc.usable:
                    out.append("  能否调试 " + self.GREEN + "可用" + self.RESET)
                else:
                    out.append("  能否调试 " + self.RED + "不可用" + self.RESET +
                               "  " + tc.unusable_reason)
                    out.append("  " + self.DIM + "按 i 或选最后一行补装一套 MinGW-w64" + self.RESET)
        if self.msg:
            out.append("")
            out.append(self.CYAN + "  " + self.msg + self.RESET)
        out.append("")
        sys.stdout.write("\n".join(out))
        sys.stdout.flush()

    def _install(self):
        """补装一套 MinGW-w64，装完重扫并直接选中它。

        列表最后一行（`i` 键也走这里）：补装过程要读键盘输入，所以先把光标
        还原、把 TUI 的隐藏状态收起来，装完再重画界面。
        """
        if not _install_supported():
            self.msg = "自动补装只支持 Windows；Linux 请用包管理器装 gcc / gdb"
            return
        sys.stdout.write(self.SHOW + "\n")
        sys.stdout.flush()
        bindir = install_toolchain(interactive=True)
        if not bindir:
            self.msg = "没有补装。也可以自己装 MSYS2 UCRT64 / MinGW-w64 后再扫一次。"
            return
        self.tcs = _rescan_with(bindir)
        key = os.path.normcase(os.path.normpath(bindir))
        idx = next((n for n, t in enumerate(self.tcs) if t.key == key), -1)
        if idx < 0:
            # 路径大小写 / 软链之类导致没并进列表：补一条，别让选择落到别的工具链上
            self.tcs.insert(0, build_toolchain(bindir, {"补装"}))
            self.tcs.sort(key=_order)
            idx = next((n for n, t in enumerate(self.tcs) if t.key == key), 0)
        self.i = idx
        # 装就装了，顺手落盘 —— 用户按 q 走人也不至于下次又挑回不可用的那套
        try:
            write_config(self.config_path, self.tcs,
                         self.tcs[self.i].key,
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
        self.msg = "已装好并选中：%s（回车确认）" % bindir

    def run(self):
        if not _stdin_is_console():
            return self.run_plain()
        try:
            while True:
                self.render()
                k = self._key()
                if k == "UP":
                    self.i = (self.i - 1) % self.row_count()
                elif k == "DOWN":
                    self.i = (self.i + 1) % self.row_count()
                elif k in ("d", "D"):
                    self.detail = not self.detail
                elif k in ("i", "I"):
                    self._install()
                elif k in ("\r", "\n"):
                    if self.on_install_row():
                        self._install()          # 选中最后一行 = 补装
                    elif not self.tcs[self.i].usable:
                        # 不让不可用的那套被静默选中：说清原因，指到补装
                        self.msg = "这套不能用来调试（%s）—— 按 i 或选最后一行补装" \
                                   % (self.tcs[self.i].unusable_reason or "原因未知")
                    else:
                        self.result = self.tcs[self.i]
                        break
                elif k in ("q", "Q", "ESC"):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            sys.stdout.write(self.SHOW + "\n")
            sys.stdout.flush()
        return self.result

    def run_plain(self):
        """非 TTY（管道 / 重定向）时降级为数字菜单。"""
        print("\n搜到 %d 套编译器：" % len(self.tcs))
        for n, tc in enumerate(self.tcs, 1):
            mark = "*" if n - 1 == self.i else " "
            print(" %s %2d) %-24s %-10s %-10s %-8s %s" % (
                mark, n, tc.name[:24], tc.family[:10], tc.version[:10],
                tc.lib_status, "可用" if tc.usable else "不可用"))
            if not tc.usable:
                print("        ↳ %s" % tc.unusable_reason)
        install_no = len(self.tcs) + 1
        if _install_supported():
            mark = "*" if self.on_install_row() else " "
            print(" %s %2d) %s" % (mark, install_no, self.install_row_label()))
        try:
            answer = input("\n输入序号回车确认（直接回车 = 保持当前选择，q = 取消）: ").strip()
        except EOFError:
            answer = ""
        if answer.lower() == "q":
            return None
        if not answer:
            return None if self.on_install_row() else (self.tcs[self.i] if self.tcs else None)
        try:
            n = int(answer)
            if n == install_no:
                print("补装要在终端里交互（会问目录、问加速），请运行：")
                print("  python cdbg.py --scan-compiler --install")
                return None
            if 1 <= n <= len(self.tcs):
                picked = self.tcs[n - 1]
                if not picked.usable:
                    print("这套不可用：%s" % picked.unusable_reason)
                    if _install_supported():
                        print("补装一套：python cdbg.py --scan-compiler --install")
                    return None
                return picked
        except ValueError:
            pass
        print("无效输入，保持原选择")
        return self.tcs[self.i] if (self.tcs and not self.on_install_row()) else None


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="四处搜刮 gcc/g++/clang/cl，写入 ./config 并用 TUI 选择")
    ap.add_argument("--config", default=None, help="配置文件路径（默认模块目录下的 config）")
    ap.add_argument("--no-tui", action="store_true", help="只扫描并写配置，不进入交互")
    ap.add_argument("--select", type=int, default=0, help="非交互直接选择第 N 套（从 1 开始）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出扫描结果后退出")
    ap.add_argument("--install", nargs="?", const="", default=None, metavar="目录",
                    help="直接补装一套 MinGW-w64（可选指定安装目录），不进入 TUI。"
                         "大陆地区默认走 gh-proxy.org 加速，CDBG_NO_ACCELERATE=1 可强制直连")
    ap.add_argument("--no-install", action="store_true",
                    help="没有可用工具链时也不问补装")
    args = ap.parse_args(argv)

    if IS_WIN and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    t0 = time.time()
    tcs = scan_all()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    config_path = args.config or DEFAULT_CONFIG_PATH

    if args.json:
        print(json.dumps([{
            "name": t.name, "family": t.family, "version": t.version,
            "exe": t.exe, "sources": t.sources, "bindir": t.bindir,
            "lib_status": t.lib_status, "missing": t.missing,
            "include_dirs": t.include_dirs, "tools": t.tools,
            "gdb_python": t.gdb_python,
            "usable": t.usable, "unusable_reason": t.unusable_reason,
        } for t in tcs], ensure_ascii=False, indent=2))
        return 0

    # 一套能调试的都没有（只有 MSVC / gdb 不带 Python / 压根没装）→ 补装
    if not any(t.usable for t in tcs) and not args.no_install:
        if args.install is not None:
            done = ensure_toolchain(tcs, interactive=False, target=args.install,
                                    config_path=config_path, force=True)
        elif not args.no_tui:
            done = ensure_toolchain(tcs, config_path=config_path)
        else:
            done = False
        if done:
            tcs = scan_all()
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elif args.install:
        # 已经有能用的，但用户点名要装到某个目录
        install_toolchain(interactive=False, target=args.install)
        tcs = scan_all()
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    previous = read_selection(config_path)
    initial = next((i for i, t in enumerate(tcs)
                    if t.exe.lower() == previous.lower()), -1)
    # 上次选的那套没了 / 变成了不可用（比如 MSVC）→ 换成第一套能用的；
    # 一套能用的都没有就落在最后那行「需要下载」上，回车即可补装。
    if initial < 0 or not tcs[initial].usable:
        initial = next((i for i, t in enumerate(tcs) if t.usable), len(tcs))
    selected = tcs[initial].key if tcs else ""

    if args.select:
        if 1 <= args.select <= len(tcs):
            selected = tcs[args.select - 1].key
    elif not args.no_tui:
        picked = Tui(tcs, initial=initial, config_path=config_path).run()
        if picked is not None:
            selected = picked.key

    write_config(config_path, tcs, selected, stamp)
    print("扫描完成：%d 套工具链，用时 %.1fs" % (len(tcs), time.time() - t0))
    print("配置文件：%s" % config_path)
    for i, t in enumerate(tcs, 1):
        flag = " ← 当前" if t.key == selected else ""
        print("  %2d. %-24s %-10s %-10s %-8s %s%s" % (
            i, t.name[:24], t.family[:10], t.version[:10], t.lib_status,
            "可用" if t.usable else "不可用", flag))
        if not t.usable:
            print("       ↳ %s" % t.unusable_reason)
    if tcs and not any(t.usable for t in tcs):
        print("没有能调试的工具链。补装一套：python cdbg.py --scan-compiler --install")
    return 0


if __name__ == "__main__":
    sys.exit(main())
