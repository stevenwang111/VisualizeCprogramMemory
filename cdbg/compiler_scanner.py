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
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
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


def _run(exe: str, args, timeout=8, stdin_data=None):
    """静默执行外部命令；任何异常都吞掉，返回 stdout/stderr 文本。"""
    kw = dict(
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", timeout=timeout,
    )
    if IS_WIN:
        kw["creationflags"] = NO_WINDOW
    if stdin_data is not None:
        kw["input"] = stdin_data
    else:
        kw["stdin"] = subprocess.DEVNULL
    try:
        r = subprocess.run([exe] + list(args), **kw)
        return r.stdout or "", r.stderr or ""
    except Exception:
        return "", ""


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

    @property
    def key(self) -> str:
        return os.path.normcase(os.path.normpath(self.bindir))

    def source_text(self) -> str:
        return ", ".join(self.sources)

    def summary(self) -> str:
        lib = "库完整" if self.lib_status == "完整" else (
            "库基本完整" if self.lib_status == "基本完整" else "库不完整")
        return "%s | %s | %s | %s | %s" % (
            self.name, self.family, self.version, self.exe, lib)


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
    out, err = _run(gdb_exe, ["--batch", "--nx", "-ex", "python print(1)"], timeout=10)
    text = (out or "") + (err or "")
    if re.search(r"(^|\n)\s*1\s*(\n|$)", text):
        return "有"
    return "无"


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
    return tc


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

    def order(tc):
        # 第一优先：gdb 带 Python。cdbg 的变量采集是注入 GDB 的 Python 脚本，
        # 没有 Python 的 gdb 能让整套东西「跑起来但看不到任何变量」。
        py = 0 if tc.gdb_python == "有" else 1
        prio = 0 if any("PATH" in s for s in tc.sources) else 1
        if any("VS Code" in s for s in tc.sources):
            prio = 0
        libp = 0 if tc.lib_status == "完整" else (1 if tc.lib_status == "基本完整" else 2)
        return (py, prio, libp, tc.name.lower())

    tcs.sort(key=order)
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
        self.i = max(0, min(initial, max(0, len(tcs) - 1)))
        self.config_path = config_path
        self.detail = True
        self.msg = ""
        self.result = None

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
        if not self.tcs:
            out.append("")
            out.append(self.YELLOW + "  没搜到任何编译器。" + self.RESET)
            out.append(self.DIM + "  试试 --json 看扫描细节，或手工用 --config 指定。" + self.RESET)
            out.append("")
            out.append("  按 q 退出")
        else:
            out.append("  " + self.DIM + "↑↓ 选择   Enter 确认   d 详情   q 退出" + self.RESET)
            out.append("")
            for n, tc in enumerate(self.tcs):
                mark = "▸" if n == self.i else " "
                color = self.GREEN if tc.lib_status == "完整" else (
                    self.YELLOW if tc.lib_status == "基本完整" else self.RED)
                lib = "库完整" if tc.lib_status == "完整" else (
                    "库基本完整" if tc.lib_status == "基本完整" else "库不全")
                row = "  %s %s  %s  %s  %s  %s" % (
                    mark, _pad("%02d" % (n + 1), 2),
                    _pad(tc.name, 24), _pad(tc.family, 11),
                    _pad(tc.version, 10), color + lib + self.RESET)
                out.append(self.REVERSE + row + self.RESET if n == self.i else row)
            if self.detail:
                tc = self.tcs[self.i]
                out.append("")
                out.append(self.DIM + "  ── 详情 ──────────────────────────────────────────" + self.RESET)
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
        if self.msg:
            out.append("")
            out.append(self.CYAN + "  " + self.msg + self.RESET)
        out.append("")
        sys.stdout.write("\n".join(out))
        sys.stdout.flush()

    def run(self):
        if not _stdin_is_console():
            return self.run_plain()
        try:
            while True:
                self.render()
                k = self._key()
                if k == "UP":
                    self.i = (self.i - 1) % max(1, len(self.tcs))
                elif k == "DOWN":
                    self.i = (self.i + 1) % max(1, len(self.tcs))
                elif k in ("d", "D"):
                    self.detail = not self.detail
                elif k in ("\r", "\n"):
                    if self.tcs:
                        self.result = self.tcs[self.i]
                        break
                    self.msg = "没有可选编译器"
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
            print(" %s %2d) %-24s %-10s %-10s %s" % (
                mark, n, tc.name[:24], tc.family[:10], tc.version[:10],
                tc.lib_status))
        try:
            answer = input("\n输入序号回车确认（直接回车 = 保持当前选择，q = 取消）: ").strip()
        except EOFError:
            answer = ""
        if answer.lower() == "q":
            return None
        if not answer:
            return self.tcs[self.i] if self.tcs else None
        try:
            n = int(answer)
            if 1 <= n <= len(self.tcs):
                return self.tcs[n - 1]
        except ValueError:
            pass
        print("无效输入，保持原选择")
        return self.tcs[self.i] if self.tcs else None


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="四处搜刮 gcc/g++/clang/cl，写入 ./config 并用 TUI 选择")
    ap.add_argument("--config", default=None, help="配置文件路径（默认模块目录下的 config）")
    ap.add_argument("--no-tui", action="store_true", help="只扫描并写配置，不进入交互")
    ap.add_argument("--select", type=int, default=0, help="非交互直接选择第 N 套（从 1 开始）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出扫描结果后退出")
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
        } for t in tcs], ensure_ascii=False, indent=2))
        return 0

    previous = read_selection(config_path)
    initial = next((i for i, t in enumerate(tcs)
                    if t.exe.lower() == previous.lower()), 0)
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
        print("  %2d. %-24s %-10s %-10s %-8s%s" % (
            i, t.name[:24], t.family[:10], t.version[:10], t.lib_status, flag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
