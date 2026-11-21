"""外部进程内存布局采集（平台门面）。

真正干活的实现按平台拆开：

- Windows：`memmap_win.py`，用 `VirtualQueryEx` 枚举已提交区域；
- Linux / WSL：`memmap_linux.py`，解析 `/proc/<pid>/maps`。

本模块只保留两边共用的数据模型、分类逻辑和统一入口，`collector.py`
完全不需要知道自己跑在哪个平台上。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import List, Optional

# --- 与 Windows 常量保持一致的区域类型（Linux 侧也复用这套取值）---
MEM_COMMIT = 0x1000
MEM_FREE = 0x10000
MEM_RESERVE = 0x2000
MEM_IMAGE = 0x1000000
MEM_MAPPED = 0x40000
MEM_PRIVATE = 0x20000

# --- 保护属性 ---
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01

#: 保护属性 → 可读性描述（Linux 的 perms 也映射到这些值）
_PROTECT_TEXT = {
    0x01: "不可访问",
    0x02: "只读",
    0x04: "可读写",
    0x08: "可写不可读",
    0x20: "可执行",
    0x40: "可执行可读",
    0x80: "可执行可读写",
}


@dataclass
class MemoryRegion:
    """一段已提交 / 已映射的内存区域。"""

    base: int
    size: int
    state: int
    protect: int
    type: int
    detail: str = ""
    tags: List[str] = field(default_factory=list)

    @property
    def end(self) -> int:
        return self.base + self.size

    @property
    def is_private(self) -> bool:
        return self.type == MEM_PRIVATE

    @property
    def is_image(self) -> bool:
        return self.type == MEM_IMAGE

    @property
    def is_mapped(self) -> bool:
        return self.type == MEM_MAPPED

    def contains(self, addr: int) -> bool:
        return self.base <= addr < self.end

    def protect_text(self) -> str:
        base = self.protect & 0xFF
        text = _PROTECT_TEXT.get(base, "0x%02X" % base)
        if self.protect & PAGE_GUARD:
            text += "+GUARD"
        return text


def _impl():
    """返回当前平台的实现模块。"""
    if sys.platform == "win32":
        from . import memmap_win

        return memmap_win
    from . import memmap_linux

    return memmap_linux


def enumerate_regions(pid: int, max_regions: int = 4096) -> List[MemoryRegion]:
    """枚举目标进程的内存区域。任何平台失败都返回空列表，不抛异常。"""
    try:
        return _impl().enumerate_regions(pid, max_regions=max_regions)
    except Exception:
        return []


def classify(
    regions: List[MemoryRegion],
    stack_pointer: Optional[int] = None,
    heap_pointers: Optional[List[int]] = None,
) -> dict:
    """把原始区域表分类成「栈 / 堆 / 映像 / 其它」四类。

    栈和堆的“归属”判定两边一致：包含 rsp 的区域是栈，包含 malloc
    返回地址的私有区域是堆。具体怎么枚举区域由平台实现负责。
    """
    heap_pointers = heap_pointers or []
    stack_regions: List[MemoryRegion] = []
    heap_regions: List[MemoryRegion] = []
    image_regions: List[MemoryRegion] = []
    other_regions: List[MemoryRegion] = []

    for reg in regions:
        if reg.is_image:
            reg.tags.append("image")
            image_regions.append(reg)
            continue

        in_stack = False
        if stack_pointer is not None and reg.contains(stack_pointer):
            in_stack = True
            reg.tags.append("stack")

        in_heap = False
        for ptr in heap_pointers:
            if ptr and reg.contains(ptr):
                in_heap = True
                reg.tags.append("heap")
                break

        if in_stack:
            stack_regions.append(reg)
        elif in_heap:
            heap_regions.append(reg)
        else:
            other_regions.append(reg)

    return {
        "stack": stack_regions,
        "heap": heap_regions,
        "image": image_regions,
        "other": other_regions,
    }


def stack_bounds(regions: List[MemoryRegion], stack_pointer: int):
    """返回包含 rsp 的区域的 (低地址, 高地址)。找不到返回 None。"""
    try:
        return _impl().stack_bounds(regions, stack_pointer)
    except Exception:
        return None


def heap_bounds(regions: List[MemoryRegion], pointers: List[int]):
    """返回所有包含堆指针的区域的 (低地址, 高地址) 列表。"""
    try:
        return _impl().heap_bounds(regions, pointers)
    except Exception:
        return []


def region_text(reg: MemoryRegion) -> str:
    """给 UI 的一句话描述。"""
    if reg.is_image:
        kind = "映像"
    elif reg.is_private:
        kind = "私有"
    elif reg.is_mapped:
        kind = "映射"
    else:
        kind = "其它"
    return "%s · %s · %d KB" % (kind, reg.protect_text(), reg.size // 1024)


def source_name() -> str:
    """区域来源的短标签，写进 RegionInfo.source（只用于展示/排查）。"""
    return "vqex" if sys.platform == "win32" else "proc-maps"


def stack_detail() -> str:
    """栈区域的详情文案。"""
    return "MEM_PRIVATE" if sys.platform == "win32" else "[stack]"


def heap_detail() -> str:
    """堆区域的详情文案。"""
    return "MEM_PRIVATE" if sys.platform == "win32" else "匿名映射 / [heap]"
