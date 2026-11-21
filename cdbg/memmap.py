"""外部进程内存布局采集（Windows）。

用 `VirtualQueryEx` 枚举目标进程的已提交内存区域，用来确定：

- **栈**的真实边界：包含当前 rsp 的那块 MEM_PRIVATE 区域
- **堆**的真实边界：包含 malloc 返回地址的那些 MEM_PRIVATE 区域
- **映像**区域：哪些是 exe / DLL 映射进来的

为什么需要它？GDB 在 Windows 上拿不到这些信息：

- `info proc mappings` —— 无输出（Linux 专有）
- `inferior.memory_regions()` —— 本环境不存在该 API

所以只能从进程外部用 ctypes 问 Windows 内核要。

参考实现要点：`OpenProcess` 的权限参数必须是
`PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ`（0x1000 | 0x10），
传错会返回 error 87（参数无效）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

# --- Windows 常量 ---
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_FREE = 0x10000
MEM_RESERVE = 0x2000
MEM_IMAGE = 0x1000000
MEM_MAPPED = 0x40000
MEM_PRIVATE = 0x20000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01

#: 保护属性 → 可读性描述
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
    """一段已提交的内存区域。"""

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


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


def _open_process(pid: int):
    """打开目标进程用于查询内存布局。"""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid
    )
    if not handle:
        return None
    kernel32.CloseHandle.restype = wt.BOOL
    return kernel32, handle


def enumerate_regions(pid: int, max_regions: int = 4096) -> List[MemoryRegion]:
    """枚举目标进程所有已提交（MEM_COMMIT）的内存区域。"""
    if sys.platform != "win32":
        return []

    opened = _open_process(pid)
    if opened is None:
        return []
    kernel32, handle = opened

    try:
        kernel32.VirtualQueryEx.argtypes = [
            wt.HANDLE,
            ctypes.c_void_p,
            ctypes.POINTER(_MBI),
            ctypes.c_size_t,
        ]
        kernel32.VirtualQueryEx.restype = ctypes.c_size_t

        regions: List[MemoryRegion] = []
        addr = 0
        while len(regions) < max_regions:
            mbi = _MBI()
            if not kernel32.VirtualQueryEx(
                handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)
            ):
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize
            if size == 0:
                break
            if mbi.State == MEM_COMMIT:
                regions.append(
                    MemoryRegion(
                        base=base,
                        size=size,
                        state=mbi.State,
                        protect=mbi.Protect,
                        type=mbi.Type,
                    )
                )
            addr = base + size
        return regions
    finally:
        kernel32.CloseHandle(handle)


def classify(
    regions: List[MemoryRegion],
    stack_pointer: Optional[int] = None,
    heap_pointers: Optional[List[int]] = None,
) -> dict:
    """把原始区域表分类成「栈 / 堆 / 映像」三类。

    返回：
        {"stack": [region...], "heap": [region...], "image": [region...], "other": [...]}
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
        elif reg.is_private:
            other_regions.append(reg)
        else:
            other_regions.append(reg)

    return {
        "stack": stack_regions,
        "heap": heap_regions,
        "image": image_regions,
        "other": other_regions,
    }


def stack_bounds(regions: List[MemoryRegion], stack_pointer: int):
    """返回包含 rsp 的区域的 (低地址, 高地址)。找不到则返回 None。

    只认私有（MEM_PRIVATE）区域 —— 栈一定是私有内存。
    如果误把 exe 的映像段当成栈，会得到完全错误的结果。
    """
    for reg in regions:
        if not reg.is_private:
            continue
        if reg.contains(stack_pointer):
            return reg.base, reg.end
    return None


def heap_bounds(regions: List[MemoryRegion], pointers: List[int]):
    """返回所有包含堆指针的私有区域的 (低地址, 高地址) 列表。

    只认私有（MEM_PRIVATE）区域：真正的堆是运行时动态分配的私有内存。
    exe 的 .data / .bss / .text 属于映像（MEM_IMAGE），不是堆。
    """
    out = []
    seen = set()
    for ptr in pointers:
        if not ptr:
            continue
        for reg in regions:
            if not reg.is_private:
                continue
            if reg.contains(ptr):
                key = (reg.base, reg.end)
                if key not in seen:
                    seen.add(key)
                    out.append((reg.base, reg.end))
                break
    return out


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
