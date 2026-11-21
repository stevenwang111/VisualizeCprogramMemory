"""Linux 内存布局采集：解析 `/proc/<pid>/maps`。

`/proc/<pid>/maps` 每行长这样：

    7ffd3c400000-7ffd3c421000 rw-p 00000000 00:00 0        [stack]
    55d4c1a2b000-55d4c1a2c000 r--p 00000000 08:02 1234567  /usr/bin/prog
    55d4c1a2c000-55d4c1a2d000 r-xp 00001000 08:02 1234567  /usr/bin/prog

和 Windows 的 `VirtualQueryEx` 相比，这里的粒度更细（一个堆可能被拆成
好几块匿名映射）。所以枚举完会先把相邻的可写匿名块合并，避免界面上
出现大量碎片分区。
"""

from __future__ import annotations

import os
from typing import List

from .memmap import (
    MEM_COMMIT,
    MEM_IMAGE,
    MEM_MAPPED,
    MEM_PRIVATE,
    MemoryRegion,
)

#: 特殊映射名：不参与堆 / 栈判定
_OTHER_SPECIAL = {"[vdso]", "[vvar]", "[vsyscall]"}

#: 可写匿名块（含 [heap]）合并时允许的类型
_MERGEABLE_DETAILS = {"", "[heap]", "匿名"}


def _protect_from_perms(perms: str) -> int:
    """把 `rw-p` 这样的权限字符串映射成 Windows 风格的 protect 值。"""
    readable = "r" in perms
    writable = "w" in perms
    executable = "x" in perms
    if executable:
        if readable and writable:
            return 0x80
        if readable:
            return 0x40
        return 0x20
    if readable and writable:
        return 0x04
    if readable:
        return 0x02
    if writable:
        return 0x08
    return 0x01


def parse_maps_text(text: str, max_regions: int = 4096) -> List[MemoryRegion]:
    """解析 `/proc/<pid>/maps` 文本。格式异常的行直接忽略。"""
    regions: List[MemoryRegion] = []

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        # 最多切 6 段：起止 / 权限 / offset / device / inode / pathname
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue
        addr_range, perms, _offset, _dev, _inode = parts[:5]
        pathname = parts[5].strip() if len(parts) > 5 else ""

        if "-" not in addr_range or len(perms) < 4:
            continue
        lo_s, _, hi_s = addr_range.partition("-")
        try:
            start = int(lo_s, 16)
            end = int(hi_s, 16)
        except ValueError:
            continue
        if end <= start:
            continue

        if pathname == "[stack]":
            kind = MEM_PRIVATE
            detail = "[stack]"
        elif pathname == "[heap]":
            kind = MEM_PRIVATE
            detail = "[heap]"
        elif pathname in _OTHER_SPECIAL:
            kind = MEM_MAPPED
            detail = pathname
        elif pathname.startswith("["):
            # 其它内核特殊映射：保留为私有区，但不当作栈
            kind = MEM_PRIVATE
            detail = pathname
        elif pathname:
            kind = MEM_IMAGE
            detail = pathname
        else:
            kind = MEM_PRIVATE
            detail = "匿名"

        regions.append(
            MemoryRegion(
                base=start,
                size=end - start,
                state=MEM_COMMIT,
                protect=_protect_from_perms(perms),
                type=kind,
                detail=detail,
            )
        )
        if len(regions) >= max_regions:
            break

    return _merge_anonymous(regions)


def _merge_anonymous(regions: List[MemoryRegion]) -> List[MemoryRegion]:
    """合并相邻的可写匿名映射，减少堆分区碎片。

    只合并「匿名 ↔ 匿名 / 匿名 ↔ [heap]」且权限相同的块；`[stack]`、
    `[vdso]` 和文件映射一律不参与，否则会把栈和堆粘成一块。
    """
    merged: List[MemoryRegion] = []
    for reg in sorted(regions, key=lambda r: r.base):
        if merged:
            prev = merged[-1]
            same_detail = (
                prev.detail in _MERGEABLE_DETAILS
                and reg.detail in _MERGEABLE_DETAILS
            )
            writable = bool(prev.protect & 0x04) and bool(reg.protect & 0x04)
            if (
                prev.type == MEM_PRIVATE
                and reg.type == MEM_PRIVATE
                and same_detail
                and writable
                and prev.protect == reg.protect
                and prev.end == reg.base
            ):
                prev.size += reg.size
                if prev.detail == "匿名" and reg.detail == "[heap]":
                    prev.detail = "[heap]"
                continue
        merged.append(reg)
    return merged


def enumerate_regions(pid: int, max_regions: int = 4096) -> List[MemoryRegion]:
    """读取 `/proc/<pid>/maps`。进程不存在 / 无权限时返回空列表。"""
    if not pid:
        return []
    try:
        with open("/proc/%d/maps" % int(pid), "r", encoding="utf-8",
                  errors="replace") as fh:
            text = fh.read()
    except (OSError, ValueError):
        return []
    return parse_maps_text(text, max_regions=max_regions)


def stack_bounds(regions: List[MemoryRegion], stack_pointer: int):
    """返回栈区域的 (低地址, 高地址)。

    优先用 `[stack]`；没有 `[stack]` 时退回「包含 rsp 的可写匿名区域」。
    """
    candidates = [
        reg for reg in regions
        if reg.detail == "[stack]" and reg.is_private
    ]
    for reg in candidates:
        if reg.contains(stack_pointer):
            return reg.base, reg.end
    if candidates:
        return candidates[0].base, candidates[0].end

    for reg in regions:
        if not reg.is_private:
            continue
        if reg.detail.startswith("["):
            continue
        if not (reg.protect & 0x04):
            continue
        if reg.contains(stack_pointer):
            return reg.base, reg.end
    return None


def heap_bounds(regions: List[MemoryRegion], pointers: List[int]):
    """返回包含堆指针的匿名 / `[heap]` 区域的 (低地址, 高地址) 列表。"""
    out = []
    seen = set()
    for ptr in pointers:
        if not ptr:
            continue
        for reg in regions:
            if not reg.is_private:
                continue
            if reg.detail == "[stack]" or (
                reg.detail.startswith("[") and reg.detail != "[heap]"
            ):
                continue
            if reg.contains(ptr):
                key = (reg.base, reg.end)
                if key not in seen:
                    seen.add(key)
                    out.append((reg.base, reg.end))
                break
    return out
