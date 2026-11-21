"""Windows 内存布局采集：`VirtualQueryEx`。

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
import sys
from typing import List

from .memmap import MEM_COMMIT, MEM_PRIVATE, MemoryRegion

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010


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


def stack_bounds(regions: List[MemoryRegion], stack_pointer: int):
    """返回包含 rsp 的私有区域的 (低地址, 高地址)。

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
