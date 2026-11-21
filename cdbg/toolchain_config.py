"""工具链配置：这一台机器上，用哪个 gcc、哪个 gdb。

分工
----
- `compiler_scanner.py` 负责「找」和「挑」：扫全盘、探测版本/头文件/gdb 的
  Python 支持，写成 `config/toolchain.conf`。
- 本模块负责「用」：把那份配置读成 cdbg 需要的东西，并在**首次开箱**时
  自动调用扫描器补上。

为什么要这一层
--------------
配置里写的是绝对路径。项目拷到另一台机器 / 另一个盘符，那些路径就全废了。
所以判断「能不能用」的标准不是「文件在不在」，而是
**「按配置里的路径能不能真的找到那个 exe」**：

    配置缺失 / 为空                        -> 扫描
    配置写了 gcc，但那个路径没有 gcc.exe    -> 扫描（换设备、卸载重装都走这条）
    配置写了 gdb，但那个路径没有 gdb.exe    -> 扫描

扫描结果会覆盖回同一份配置，所以「开箱即用」在第二台机器上同样成立。
进程内只扫一次（`_CACHE`），不会每个命令都重新探一遍。
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Optional

from .compiler_scanner import DEFAULT_CONFIG_PATH, scan_all, write_config

#: 配置文件路径（<项目根>/config/toolchain.conf）
CONFIG_PATH = DEFAULT_CONFIG_PATH

_CACHE: Optional[dict] = None
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 读配置
# ---------------------------------------------------------------------------


def config_exists() -> bool:
    return os.path.isfile(CONFIG_PATH) and os.path.getsize(CONFIG_PATH) > 0


def _parse_sections(text: str):
    """把 `[段名]` + `键 = 值` 的配置读成 {段名: {键: 值}}。

    段名允许重复出现（扫描器就是这么写的），这里后出现的覆盖先出现的 ——
    正好是「最后一次选择生效」的语义。
    """
    sections = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        if current is not None and "=" in line:
            key, value = line.split("=", 1)
            current[key.strip()] = value.strip()
    return sections


def read_selected(path: Optional[str] = None) -> dict:
    """读出「设置项2-当前编译器」这一节。读不到就返回空字典。"""
    target = path or CONFIG_PATH
    try:
        with open(target, "r", encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {}

    sections = _parse_sections(text)
    for name, body in sections.items():
        if name.startswith("设置项2"):
            return body
    return {}


def read_all(path: Optional[str] = None) -> list:
    """读出「设置项1」里每条摘要（只给命令行展示用）。"""
    target = path or CONFIG_PATH
    try:
        with open(target, "r", encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []
    out = []
    for name, body in _parse_sections(text).items():
        if name.startswith("设置项1"):
            for key in sorted(body):
                if key.startswith("编译器"):
                    out.append(body[key])
    return out


# ---------------------------------------------------------------------------
# 判断可用性
# ---------------------------------------------------------------------------


def _exe_of(sel: dict, key: str) -> str:
    """取字段里的 exe 路径：`gcc` 优先，退回 `主程序`。"""
    if key == "gcc":
        return sel.get("gcc") or sel.get("主程序") or ""
    return sel.get(key) or ""


def check(sel: dict):
    """配置能不能直接用。返回 (可用?, 不能用的原因)。"""
    if not sel:
        return False, "配置文件里没有「当前编译器」"
    gcc = _exe_of(sel, "gcc")
    if not gcc:
        return False, "配置里没写 gcc 路径"
    if not os.path.isfile(gcc):
        return False, "配置里的 gcc 在这台机器上不存在：%s" % gcc
    gdb = _exe_of(sel, "gdb")
    if not gdb:
        return False, "配置里没写 gdb 路径（调试器需要它）"
    if not os.path.isfile(gdb):
        return False, "配置里的 gdb 在这台机器上不存在：%s" % gdb
    return True, ""


# ---------------------------------------------------------------------------
# 首次开箱：扫描 + 自动选一套
# ---------------------------------------------------------------------------


def _say(message: str = "") -> None:
    try:
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _pick(tcs):
    """从扫描结果里挑一套：能编译 + 能调试 + gdb 带 Python，越好越靠前。"""
    def usable(tc):
        return bool(tc.exe) and os.path.isfile(tc.exe)

    def has_gdb(tc):
        gdb = (tc.tools or {}).get("gdb") or ""
        return bool(gdb) and os.path.isfile(gdb)

    for wanted in (
        lambda tc: usable(tc) and has_gdb(tc) and tc.gdb_python == "有",
        lambda tc: usable(tc) and has_gdb(tc),
        usable,
    ):
        for tc in tcs:
            if wanted(tc):
                return tc
    return None


def scan_and_write(interactive: bool = False, select: int = 0,
                   quiet: bool = False) -> Optional[dict]:
    """跑一次扫描并写配置，返回被选中的那一节。找不到编译器时返回 None。

    interactive=True 时进入 TUI（`cdbg --scan-compiler`），否则按优先级自动选。
    """
    import datetime

    _say("[工具链] 正在扫描本机编译器（首次开箱 / 配置失效时会自动做这件事）...")
    try:
        tcs = scan_all()
    except Exception as exc:  # 扫描器自身出问题也不能带崩调试器
        _say("[工具链] 扫描失败：%s" % exc)
        return None

    if not tcs:
        _say("[工具链] 没找到任何 C 编译器。")
        if os.name == "nt":
            _say("         请安装 MSYS2 UCRT64 / MinGW-w64，或设置环境变量 CDBG_CC 指向 gcc.exe。")
        else:
            _say("         请安装：sudo apt install gcc gdb，或设置环境变量 CDBG_CC 指向 gcc。")
        return None

    picked = None
    if interactive:
        from .compiler_scanner import Tui

        previous = read_selected().get("主程序", "")
        initial = next(
            (i for i, t in enumerate(tcs) if t.exe.lower() == previous.lower()), 0
        )
        picked = Tui(tcs, initial=initial, config_path=CONFIG_PATH).run()
    elif select:
        if 1 <= select <= len(tcs):
            picked = tcs[select - 1]
    if picked is None:
        picked = _pick(tcs)
    if picked is None:
        _say("[工具链] 找到了编译器，但没有一套同时带 gcc 和 gdb，无法调试。")
        return None

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        write_config(CONFIG_PATH, tcs, picked.key, stamp)
    except Exception as exc:
        _say("[工具链] 写配置失败：%s" % exc)
        # 配置写不下去也还能继续跑 —— 直接用内存里这套
        return _as_dict(picked)

    sel = read_selected()
    if not quiet:
        _show_picked(picked, len(tcs))
    return sel or _as_dict(picked)

def _as_dict(tc) -> dict:
    """Toolchain -> 与配置文件同名的字典，供不用落盘时兜底。"""
    return {
        "名称": tc.name,
        "编译器类型": tc.family,
        "版本号": tc.version,
        "主程序": tc.exe,
        "是否携带基础运行库": tc.lib_status,
        "gdb的Python支持": tc.gdb_python,
        "gcc": (tc.tools or {}).get("gcc", "") or tc.exe,
        "gdb": (tc.tools or {}).get("gdb", ""),
    }


def _show_picked(tc, total: int) -> None:
    _say("[工具链] 找到 %d 套，已选用：%s · %s %s"
         % (total, tc.name, tc.family, tc.version))
    _say("         gcc = %s" % ((tc.tools or {}).get("gcc") or tc.exe))
    _say("         gdb = %s（Python 支持：%s）"
         % ((tc.tools or {}).get("gdb") or "无", tc.gdb_python))
    _say("         配置 = %s" % CONFIG_PATH)
    if tc.gdb_python == "无":
        _say("         警告：这套 gdb 没有 Python 支持，调试时采集不到变量。")
        _say("               换一套：python cdbg.py --scan-compiler")
    else:
        _say("         想换一套：python cdbg.py --scan-compiler")


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def resolve(force: bool = False) -> dict:
    """拿到本次进程要用的工具链。必要时自动扫描一次。

    返回 {"selected": {...}, "gcc": path, "gdb": path, "scanned": bool}
    gcc/gdb 可能是空字符串（扫描也没找到），调用方决定要不要报错。
    """
    global _CACHE
    with _LOCK:
        if _CACHE is not None and not force:
            return _CACHE

        sel = read_selected()
        ok, reason = check(sel)
        scanned = False
        if not ok:
            if config_exists():
                _say("[工具链] 配置不可用：%s" % reason)
            else:
                _say("[工具链] 还没有工具链配置（%s）" % CONFIG_PATH)
            sel = scan_and_write(quiet=False) or {}
            scanned = True

        _CACHE = {
            "selected": sel or {},
            "gcc": _exe_of(sel or {}, "gcc"),
            "gdb": _exe_of(sel or {}, "gdb"),
            "scanned": scanned,
        }
        return _CACHE


def gcc_path() -> str:
    """配置里指定的 gcc（找不到就返回空字符串）。"""
    path = resolve().get("gcc") or ""
    return path if path and os.path.isfile(path) else ""


def gdb_path() -> str:
    """配置里指定的 gdb（找不到就返回空字符串）。"""
    path = resolve().get("gdb") or ""
    return path if path and os.path.isfile(path) else ""


def describe() -> str:
    """一行摘要，给 `--toolchain` 用。"""
    res = resolve()
    sel = res.get("selected") or {}
    if not res.get("gcc"):
        return "未配置（没找到可用的 C 编译器）"
    return "%s · %s %s\n    gcc = %s\n    gdb = %s\n    配置 = %s" % (
        sel.get("名称", "未知"),
        sel.get("编译器类型", "?"),
        sel.get("版本号", "?"),
        res["gcc"],
        res.get("gdb") or "（没有）",
        CONFIG_PATH,
    )
