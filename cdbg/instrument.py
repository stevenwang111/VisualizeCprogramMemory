"""源码插桩：把堆跟踪的 shim 注入用户源码。

## 插桩原则

**绝不改变原程序的语义。** 这是硬约束。具体保障：

1. **只插入一行 `#include`** —— 用户写的代码一行不改。
2. **shim.h 先 include 标准库、再定义宏** —— 这样标准库里的
   `malloc` 声明不会被宏污染，不会有任何编译错误。
3. **插入点选在「最后一个 #include 之后」** —— 用户代码里对堆函数的
   调用全部落在宏生效期内，而标准库头已经加载完毕。
4. **检测到用户自己定义了 malloc / free 时直接放弃插桩** —— 绝不硬来。
5. **插桩后编译失败会自动降级为不插桩重编一次** —— 插桩永远只是锦上添花，
   出了问题也不能影响基本的调试功能。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

SHIM_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shim")

#: 匹配用户自己定义 malloc / free 的语句（函数定义形式）
_USER_MALLOC_DEF = re.compile(
    r"^\s*(?:[\w\*\s]+?\s+)?\bmalloc\s*\([^;]*\)\s*\{", re.MULTILINE
)
_USER_FREE_DEF = re.compile(
    r"^\s*(?:[\w\*\s]+?\s+)?\bfree\s*\([^;]*\)\s*\{", re.MULTILINE
)

#: 匹配 #include 指令
_INCLUDE_RE = re.compile(r"^\s*#\s*include\b", re.MULTILINE)

#: 匹配真正 include 了 shim 的语句。
#: 必须锚定行首的 #include —— 否则注释里提到 "cdbg_shim.h" 也会被误判。
_SHIM_INCLUDE_RE = re.compile(
    r"^[ \t]*#[ \t]*include[ \t]*[\"<]cdbg_shim\.h[\">]", re.MULTILINE
)


@dataclass
class InstrumentResult:
    """一次插桩的结果。"""

    ok: bool
    text: str = ""
    """插入后的源码文本（ok 为 False 时是原文）。"""

    notes: List[str] = None
    """插桩过程中的说明（会显示给用户）。"""

    reason: str = ""
    """放弃插桩的原因。"""

    line_offset: int = 0
    """插桩使源码整体下移的行数。

    插桩点在插入位置之后的所有行都会 +offset。
    调试器必须把这个偏移减回去，否则界面上的行号会和用户看到的
    原始源码对不上 —— 这是插桩类工具最容易出的错。
    """

    def __post_init__(self):
        if self.notes is None:
            self.notes = []


def shim_paths() -> Tuple[str, str]:
    """返回 (shim 头, shim 实现) 的路径。"""
    return (
        os.path.join(SHIM_DIR, "cdbg_shim.h"),
        os.path.join(SHIM_DIR, "cdbg_shim.c"),
    )


def _find_insert_position(text: str) -> int:
    """找到合适的插入位置。

    策略：最后一个 #include 之后。如果找不到 #include，就插在文件开头
    （第一段注释/预处理指令之前）。
    """
    last = 0
    for match in _INCLUDE_RE.finditer(text):
        # 找到这一行结尾
        end = text.find("\n", match.end())
        last = end + 1 if end >= 0 else len(text)
    if last:
        return last
    # 没有 include：插到文件最前面（跳过开头的 BOM / 注释头）
    return 0


def instrument_text(text: str) -> InstrumentResult:
    """对源码文本做插桩。"""
    notes: List[str] = []

    # ---- 安全检查 1：用户自己定义了堆函数 ----
    if _USER_MALLOC_DEF.search(text) or _USER_FREE_DEF.search(text):
        return InstrumentResult(
            ok=False,
            text=text,
            notes=notes,
            reason="用户代码里自定义了 malloc/free，为避免语义冲突，跳过堆跟踪插桩",
        )

    # ---- 安全检查 2：已经插过桩 ----
    if _SHIM_INCLUDE_RE.search(text):
        return InstrumentResult(
            ok=False, text=text, notes=notes, reason="源码里已经有 cdbg_shim.h，跳过重复插桩"
        )

    pos = _find_insert_position(text)
    snippet = '\n/* --- cdbg 堆跟踪插桩（自动插入，不影响程序逻辑） --- */\n#include "cdbg_shim.h"\n'
    new_text = text[:pos] + snippet + text[pos:]
    offset = snippet.count("\n")
    notes.append("已在第 %d 行插入堆跟踪插桩" % (text[:pos].count("\n") + 1))
    return InstrumentResult(ok=True, text=new_text, notes=notes, line_offset=offset)


def prepare_build(
    source: str,
    workdir: str,
    text: str,
    enable: bool = True,
    heap_log_path: Optional[str] = None,
) -> Tuple[str, List[str], List[str], int]:
    """准备编译所需的输入。

    返回 (写入工作目录的源码文本, 需要额外参与编译的源文件, 说明信息, 行号偏移)。
    """
    notes: List[str] = []
    header_path, impl_path = shim_paths()

    if not enable:
        return text, [], ["未启用堆跟踪插桩"], 0

    result = instrument_text(text)
    if not result.ok:
        notes.append("跳过堆跟踪：" + result.reason)
        return text, [], notes, 0

    # 把 shim 头复制到工作目录，源码里的 #include "cdbg_shim.h" 才能找到
    dst_header = os.path.join(workdir, "cdbg_shim.h")
    dst_impl = os.path.join(workdir, "cdbg_shim.c")
    try:
        with open(header_path, "r", encoding="utf-8") as fh:
            header_text = fh.read()
        with open(dst_header, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(header_text)
        with open(impl_path, "r", encoding="utf-8") as fh:
            impl_text = fh.read()

        # 把账本路径写死进编译产物。
        # GDB 的 `set environment` 在 Windows 上不可靠（实测被调试程序
        # 读不到环境变量），所以路径必须落到编译期常量上。
        if heap_log_path:
            safe = heap_log_path.replace("\\", "/").replace('"', '\\"')
            impl_text = '#define CDBG_HEAP_LOG_PATH "%s"\n' % safe + impl_text

        with open(dst_impl, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(impl_text)
    except OSError as exc:
        notes.append("复制 shim 失败，跳过堆跟踪：%s" % exc)
        return text, [], notes, 0

    notes.extend(result.notes)
    notes.append("堆跟踪已启用（记录每次 malloc / free 的地址、大小与来源行号）")
    return result.text, [dst_impl], notes, result.line_offset
