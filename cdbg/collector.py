"""采集聚合层。

把两个来源的数据汇成一份统一的 Snapshot：

1. **GDB 探针**（gdb_probe.py）：变量、栈帧、字节、段边界 —— 进程内部视角
2. **memmap**（memmap.py）：真实堆 / 栈区域边界 —— 进程外部视角

然后由 curriculum 补上教学提示。

聚合这一层还负责把「四个教学分区」拼装出来：程序段 → 全局与静态 → 堆 → 栈。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from . import curriculum, heap_ledger, memmap
from .snapshot import (
    KIND_BSS,
    KIND_DATA,
    KIND_HEAP,
    KIND_RDATA,
    KIND_STACK,
    KIND_TEXT,
    TEACHING_ORDER,
    FrameInfo,
    HeapBlock,
    MemChunk,
    RegionInfo,
    Snapshot,
    VarInfo,
)


class Collector:
    """把探针原始数据加工成前端可直接消费的 Snapshot。"""

    def __init__(self, program: str = "", heap_log_path: Optional[str] = None):
        self.program = program
        self.heap_log_path = heap_log_path
        #: 插桩造成的行号偏移，采集到的行号要减掉它才对得上原始源码
        self.line_offset = 0
        self._seq = 0
        self._prev: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def build(self, raw: Dict[str, Any]) -> Snapshot:
        """把探针的原始 JSON 变成 Snapshot。"""
        self._seq += 1
        snap = Snapshot()
        snap.seq = self._seq
        snap.program = self.program or raw.get("program", "")
        snap.state = raw.get("state", "stopped")
        snap.reason = raw.get("reason", "")
        snap.pid = raw.get("pid")
        snap.warnings = list(raw.get("warnings", []))

        # ---- 行号回退：插桩后的行号要还原成原始源码的行号 ----
        off = self.line_offset

        def fix_line(value):
            if off and value:
                return max(0, value - off)
            return value

        # ---- 帧 ----
        snap.frames = [
            FrameInfo(
                index=f.get("index", i),
                func=f.get("func", "?"),
                file=f.get("file", ""),
                line=fix_line(f.get("line", 0)),
                pc=f.get("pc", 0),
                rsp=f.get("rsp", 0),
                rbp=f.get("rbp", 0),
                low=f.get("low", 0),
                high=f.get("high", 0),
                size=f.get("size", 0),
                vars=[VarInfo(**v) for v in f.get("vars", [])],
            )
            for i, f in enumerate(raw.get("frames", []))
        ]
        for fr in snap.frames:
            for v in fr.vars:
                if v.decl_line:
                    v.decl_line = fix_line(v.decl_line)

        snap.depth = raw.get("depth", len(snap.frames))
        snap.stack_bytes = raw.get("stack_bytes", sum(f.size for f in snap.frames))

        if snap.frames:
            snap.current_file = snap.frames[0].file
            snap.current_line = snap.frames[0].line
            snap.current_func = snap.frames[0].func
        snap.source_file = self._primary_source(snap)

        # ---- 全局与静态变量 ----
        snap.globals = [VarInfo(**g) for g in raw.get("globals", [])]
        for g in snap.globals:
            if g.decl_line:
                g.decl_line = fix_line(g.decl_line)

        # ---- 字节窗口 ----
        snap.chunks = [MemChunk(**c) for c in raw.get("chunks", [])]

        # ---- 段 ----
        sections = raw.get("regions", [])
        snap.regions = self._build_regions(sections, snap)

        # ---- 堆 ----
        snap.heap = self._build_heap(snap)

        # ---- 诊断 ----
        self._diagnose(snap)

        # ---- 教学提示 ----
        try:
            snap.lessons = curriculum.analyze(
                frames=[f.to_json() for f in snap.frames],
                globals_=[g.to_json() for g in snap.globals],
                regions=[r.to_json() for r in snap.regions],
                depth=snap.depth,
                stack_bytes=snap.stack_bytes,
            )
        except Exception as exc:
            snap.warnings.append("教学提示生成失败: %s" % str(exc)[:100])

        self._prev = raw
        return snap

    # ------------------------------------------------------------------
    # 分区组装
    # ------------------------------------------------------------------

    def _build_regions(self, sections: List[Dict[str, Any]], snap: Snapshot) -> List[RegionInfo]:
        """组装四个教学分区。

        顺序按教材教法：程序段 → 全局与静态 → 堆 → 栈。
        注意：Windows 上真实地址分布与教材示意图相反（代码段在高位、栈在低位），
        所以这里刻意按「教学逻辑」排序，真实地址标注在每个区块上。
        """
        regions: List[RegionInfo] = []
        members: Dict[str, List[str]] = {}

        # 变量归属
        #
        # 帧里的 `static` 变量（如函数内 `static int c;`）存储区不在栈上，
        # 要归到 .data / .bss，否则分区标题的成员列表里看不到它。
        # 同一个名字可能出现两次（全局变量在帧的可见变量里也会出现），去重。
        for g in snap.globals:
            if g.storage in (KIND_DATA, KIND_BSS, KIND_RDATA):
                members.setdefault(g.storage, []).append(g.name)
        for fr in snap.frames:
            for v in fr.vars:
                if v.storage in (KIND_DATA, KIND_BSS, KIND_RDATA):
                    members.setdefault(v.storage, []).append(v.name)
                elif v.storage == KIND_STACK:
                    members.setdefault(KIND_STACK, []).append(v.name)
        for names in members.values():
            seen_names = set()
            keep = []
            for name in names:
                if name not in seen_names:
                    seen_names.add(name)
                    keep.append(name)
            names[:] = keep

        # 主 exe 的段
        for sec in sections:
            kind = sec.get("kind", "data")
            if kind not in (KIND_TEXT, KIND_DATA, KIND_RDATA, KIND_BSS):
                continue
            regions.append(
                RegionInfo(
                    kind=kind,
                    label=_label_of(kind, sec.get("name", "")),
                    start=sec.get("start", 0),
                    end=sec.get("end", 0),
                    source="gdb-sections",
                    members=members.get(kind, []),
                    detail=sec.get("name", ""),
                )
            )

        # 栈与堆的真实边界（来自 Windows 内核）
        stack_bounds = self._stack_bounds(snap)

        heap_bounds = self._heap_bounds(snap)
        for lo, hi in heap_bounds:
            # 排除与栈重叠的区域（同一块内存不该既是堆又是栈）
            if stack_bounds and lo < stack_bounds[1] and hi > stack_bounds[0]:
                continue
            # 排除与 exe 段重叠的区域（映像内存不是堆）
            if any(lo < r.end and hi > r.start for r in regions):
                continue
            regions.append(
                RegionInfo(
                    kind=KIND_HEAP,
                    label="堆（动态分配）",
                    start=lo,
                    end=hi,
                    source=memmap.source_name(),
                    members=[],
                    detail=memmap.heap_detail(),
                )
            )

        if stack_bounds:
            lo, hi = stack_bounds
            regions.append(
                RegionInfo(
                    kind=KIND_STACK,
                    label="栈（函数调用时压栈）",
                    start=lo,
                    end=hi,
                    source=memmap.source_name(),
                    members=members.get(KIND_STACK, []),
                    detail=memmap.stack_detail(),
                )
            )

        # 按教学顺序排
        regions.sort(key=lambda r: TEACHING_ORDER.index(r.kind) if r.kind in TEACHING_ORDER else 99)
        return regions

    def _heap_bounds(self, snap: Snapshot) -> List[tuple]:
        """堆的真实边界。

        优先用被监控程序自己 malloc 出来的地址去反查所属区域 —— 这比
        猜测「某个大小的私有区就是堆」可靠得多。
        指向 exe 自身段（.data/.text 等）或者栈的指针都要排除掉，
        否则会把代码段误判成堆。
        """
        ptrs = []
        for g in snap.globals:
            if g.is_pointer and g.pointer_target:
                ptrs.append(g.pointer_target)
        for fr in snap.frames:
            for v in fr.vars:
                if v.is_pointer and v.pointer_target:
                    ptrs.append(v.pointer_target)

        if not ptrs or not snap.pid:
            return []

        # 排除落在 exe 段内、以及落在栈内的指针
        exe_ranges = [(r.start, r.end) for r in snap.regions
                      if r.kind in (KIND_TEXT, KIND_DATA, KIND_BSS, KIND_RDATA)]
        stack_range = None
        for fr in snap.frames:
            if fr.low and fr.high:
                stack_range = (fr.low, fr.high)
                break

        candidates = []
        for ptr in ptrs:
            if any(lo <= ptr < hi for lo, hi in exe_ranges):
                continue
            if stack_range and stack_range[0] <= ptr < stack_range[1]:
                continue
            candidates.append(ptr)

        if not candidates:
            return []

        regions = memmap.enumerate_regions(snap.pid)
        if not regions:
            return []
        return memmap.heap_bounds(regions, candidates)

    def _stack_bounds(self, snap: Snapshot):
        """栈的真实边界：包含当前 rsp 的私有区域。"""
        if not snap.frames or not snap.pid:
            return None
        rsp = snap.frames[0].rsp
        if not rsp:
            return None
        regions = memmap.enumerate_regions(snap.pid)
        if not regions:
            return None
        return memmap.stack_bounds(regions, rsp)

    def _build_heap(self, snap: Snapshot) -> List[HeapBlock]:
        """确定堆块。

        优先用插桩账本 —— 它能给出精确的块边界、大小、分配点，
        以及哪些块已经被释放。没有账本时退化为「从指针反查推断」。
        """
        blocks: List[HeapBlock] = []
        seen = set()

        # ---- 路径一：插桩账本（精确）----
        if self.heap_log_path:
            ledger = heap_ledger.parse(self.heap_log_path)
            for entry in ledger.live_blocks:
                if entry.address in seen:
                    continue
                seen.add(entry.address)
                blocks.append(
                    HeapBlock(
                        address=entry.address,
                        size=entry.size,
                        label="%d 字节 @ %s:%d" % (
                            entry.size, entry.file, self._fix_line(entry.line)),
                        allocated=True,
                        site="%s:%d" % (entry.file, self._fix_line(entry.line)),
                    )
                )
            if ledger.blocks:
                snap.heap_freed = [
                    HeapBlock(
                        address=e.address,
                        size=e.size,
                        label=e.label,
                        allocated=False,
                        site=e.freed_at,
                    )
                    for e in ledger.blocks
                    if e.freed
                ]
                snap.heap_total = ledger.total_bytes
            if ledger.errors:
                snap.warnings.extend(ledger.errors)

        if blocks:
            return blocks

        # ---- 路径二：从指针反查推断 ----
        def consider(v: VarInfo, owner: str):
            if not v.is_pointer or not v.pointer_target:
                return
            # 目标落在栈 / 全局区 → 那不是堆
            for reg in snap.regions:
                if reg.kind in (KIND_STACK, KIND_DATA, KIND_BSS, KIND_RDATA):
                    if reg.start <= v.pointer_target < reg.end:
                        return
            if v.pointer_target in seen:
                return
            seen.add(v.pointer_target)
            blocks.append(
                HeapBlock(
                    address=v.pointer_target,
                    size=0,
                    label="%s 指向的内存（大小未知）" % owner,
                    allocated=True,
                    site=owner,
                )
            )

        for g in snap.globals:
            consider(g, g.name)
        for fr in snap.frames:
            for v in fr.vars:
                consider(v, v.name)
        return blocks

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    def _diagnose(self, snap: Snapshot) -> None:
        """标注常见问题：未初始化、空指针、悬垂指针、越界。"""
        known = [
            (r.start, r.end)
            for r in snap.regions
            if r.kind in (KIND_STACK, KIND_DATA, KIND_BSS, KIND_RDATA, KIND_HEAP)
        ]

        def check(v: VarInfo, ref_line: Optional[int] = None):
            # 是否已经执行到它的初始化语句。
            # 参考行必须用「变量所属那个帧」的当前行 —— 用全局当前行会误判：
            # 递归时最内层帧的行号很小，main 帧里早已初始化的变量
            # 会被错误地标成「未初始化」。
            if v.scope in ("local", "arg") and v.decl_line is not None:
                line = ref_line if ref_line else snap.current_line
                # GDB 停在某行时表示「该行尚未执行」，所以声明行 >= 当前行
                # 就意味着这个变量还没被赋值。
                if v.decl_line >= line:
                    v.initialized = False
                    v.issues.append(
                        "尚未初始化：程序还没执行到第 %d 行的赋值语句，"
                        "这里显示的是栈上残留的旧数据" % v.decl_line
                    )

            if not v.is_pointer:
                return
            if v.pointer_target is None:
                return
            if v.pointer_target == 0:
                v.issues.append("空指针：还没有指向任何东西（NULL）")
                return
            inside = any(lo <= v.pointer_target < hi for lo, hi in known)
            if not inside:
                v.issues.append(
                    "悬垂指针：它指向的地址 0x%016X 不在任何已知区域里，"
                    "很可能指向了已释放的内存" % v.pointer_target
                )

        for g in snap.globals:
            check(g)
        for fr in snap.frames:
            for v in fr.vars:
                check(v, fr.line)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _fix_line(self, value: int) -> int:
        """把插桩后的行号还原成原始源码行号。"""
        if self.line_offset and value:
            return max(0, value - self.line_offset)
        return value

    @staticmethod
    def _primary_source(snap: Snapshot) -> str:
        """猜测用户的主源码文件（优先非系统路径）。"""
        for fr in snap.frames:
            if fr.file and "windows" not in fr.file.lower():
                return fr.file
        for g in snap.globals:
            pass
        return snap.current_file


def _label_of(kind: str, section_name: str) -> str:
    return {
        KIND_TEXT: "程序段（代码 · %s）" % section_name,
        KIND_DATA: "全局与静态变量（已初始化 · %s）" % section_name,
        KIND_RDATA: "只读数据（字符串常量 · %s）" % section_name,
        KIND_BSS: "全局与静态变量（未初始化 · %s）" % section_name,
    }.get(kind, kind)
