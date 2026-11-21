"""Snapshot 数据模型 —— 调试器状态的统一数据结构。

整条链路的数据流是单向的：

    GDB 采集(gdb_probe) ──JSON文件──> Collector ──Snapshot──> Web ──JSON──> 前端

所以这里的 dataclass 既是采集端的生产者，也是消费端的契约。
前端只认 JSON，不认 Python 对象——因此所有结构必须可无损序列化为 JSON。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 区域种类（对应 C 程序的内存布局）
# ---------------------------------------------------------------------------

KIND_TEXT = "text"  # 程序段 / 代码段
KIND_DATA = "data"  # 已初始化的全局与静态变量
KIND_RDATA = "rdata"  # 只读数据：字符串常量、const 全局
KIND_BSS = "bss"  # 未初始化的全局与静态变量
KIND_HEAP = "heap"  # 堆
KIND_STACK = "stack"  # 栈

#: 教学逻辑顺序（教材里的经典顺序：程序段 → 全局静态 → 堆 → 栈）
TEACHING_ORDER = [KIND_TEXT, KIND_DATA, KIND_RDATA, KIND_BSS, KIND_HEAP, KIND_STACK]

#: 区域中文名
KIND_LABELS = {
    KIND_TEXT: "程序段（代码）",
    KIND_DATA: "全局与静态变量（已初始化）",
    KIND_RDATA: "只读数据（字符串常量 / const）",
    KIND_BSS: "全局与静态变量（未初始化）",
    KIND_HEAP: "堆（动态分配的内存）",
    KIND_STACK: "栈（函数调用时的局部变量）",
}

#: 存储位置在教学上的含义 —— 第 4 章「变量的存储类型」的核心
STORAGE_EXPLAIN = {
    KIND_DATA: "自动变量不会出现在这里；这里的变量在程序启动前就已分配，生命周期到程序结束。",
    KIND_BSS: "未初始化的全局/静态变量被操作系统清零，所以初值是 0 而不是随机值。",
    KIND_RDATA: "字符串字面量写在这里，程序运行期间只读 —— 往这里写会崩溃。",
    KIND_TEXT: "你写的代码编译成机器指令放在这里，通常只读且可执行。",
    KIND_HEAP: "malloc 申请的内存，运行时动态分配，由程序员负责释放。",
    KIND_STACK: "函数调用时自动变量和参数放在这里，函数返回就被回收，遵循后进先出。",
}


# ---------------------------------------------------------------------------
# 变量
# ---------------------------------------------------------------------------


@dataclass
class VarInfo:
    """一个变量（含指针）的完整信息。"""

    name: str
    type: str
    size: int
    address: Optional[int] = None
    value: str = ""
    #: "global" | "static" | "local" | "arg"
    scope: str = "local"
    #: 所属栈帧深度；全局变量为 None
    frame: Optional[int] = None
    #: 是否指针类型
    is_pointer: bool = False
    #: 指针指向的目标地址（仅当 is_pointer 且能解析）
    pointer_target: Optional[int] = None
    #: 指针指向的目标所对应的变量名（若能解析）
    pointer_target_name: Optional[str] = None
    #: 落在哪个内存区域：text/data/rdata/bss/heap/stack
    storage: str = "unknown"
    #: 数组的元素个数（若为数组）
    array_length: Optional[int] = None
    #: 是否可解引用为字符串
    is_string: bool = False
    #: 数组/字符串的预览值
    preview: Optional[str] = None
    #: 变量声明所在的源码行（局部变量用来判断是否已执行到赋值语句）
    decl_line: Optional[int] = None
    #: 是否已经执行到它的初始化语句
    initialized: bool = True
    #: 诊断信息：越界、空指针、悬垂指针等
    issues: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 栈帧
# ---------------------------------------------------------------------------


@dataclass
class FrameInfo:
    """一个调用栈帧。"""

    index: int
    func: str
    file: str = ""
    line: int = 0
    pc: int = 0
    rsp: int = 0
    rbp: int = 0
    #: 栈帧的地址范围（低地址 .. 高地址）。Windows 上栈向低地址增长。
    low: int = 0
    high: int = 0
    #: 本帧内的变量
    vars: List[VarInfo] = field(default_factory=list)
    #: 本帧占用的字节数
    size: int = 0

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d["vars"] = [v.to_json() if isinstance(v, VarInfo) else v for v in self.vars]
        return d


# ---------------------------------------------------------------------------
# 内存区域与字节窗口
# ---------------------------------------------------------------------------


@dataclass
class RegionInfo:
    """一个内存区域（一个段，或一块堆/栈）。"""

    kind: str
    label: str
    start: int
    end: int
    #: 数据来源："gdb-sections" | "vqex" | "inferred"
    source: str = "inferred"
    #: 该区域内的变量名列表
    members: List[str] = field(default_factory=list)
    #: 保护属性 / 描述信息（来自 VirtualQueryEx）
    detail: str = ""

    @property
    def size(self) -> int:
        return max(0, self.end - self.start)

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d["size"] = self.size
        return d


@dataclass
class MemChunk:
    """一段连续的原始字节，供 HEX 网格渲染。"""

    kind: str
    start: int
    #: 十六进制字符串，每两个字符一个字节
    data: str = ""
    #: 该窗口的来源说明，例如 "栈帧 #2 (fact)"、"全局变量区"
    label: str = ""
    #: 该窗口归属的变量（若整块属于一个变量）
    owner: Optional[str] = None

    @property
    def size(self) -> int:
        return len(self.data) // 2

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d["size"] = self.size
        return d


# ---------------------------------------------------------------------------
# 堆
# ---------------------------------------------------------------------------


@dataclass
class HeapBlock:
    """一个堆块（来自插桩账本，或从指针反查推断）。"""

    address: int
    size: int
    label: str = ""
    allocated: bool = True
    #: 分配点所在源码位置
    site: str = ""

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 教学提示
# ---------------------------------------------------------------------------


@dataclass
class Lesson:
    """一条与当前程序状态关联的教材知识点提示。"""

    chapter: str
    topic: str
    message: str
    #: 关联的变量名 / 内存地址，前端用于高亮
    refs: List[Dict[str, Any]] = field(default_factory=list)
    #: 提示级别："info" | "tip" | "warn"
    level: str = "info"

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    """某一时刻被调试程序的完整状态。"""

    seq: int = 0
    timestamp: float = field(default_factory=time.time)
    #: "running" | "stopped" | "exited"
    state: str = "stopped"
    #: 停止原因："breakpoint-hit" | "step" | "entry" | "signal" | "exited"
    reason: str = ""
    program: str = ""
    pid: Optional[int] = None
    source_file: str = ""
    current_file: str = ""
    current_line: int = 0
    current_func: str = ""
    frames: List[FrameInfo] = field(default_factory=list)
    #: 全局与静态变量（不含栈内变量）
    globals: List[VarInfo] = field(default_factory=list)
    regions: List[RegionInfo] = field(default_factory=list)
    chunks: List[MemChunk] = field(default_factory=list)
    heap: List[HeapBlock] = field(default_factory=list)
    #: 已被释放的堆块（来自插桩账本）
    heap_freed: List[HeapBlock] = field(default_factory=list)
    #: 当前活跃堆内存总量
    heap_total: int = 0
    lessons: List[Lesson] = field(default_factory=list)
    #: 递归深度（栈帧数）
    depth: int = 0
    #: 栈内存总占用
    stack_bytes: int = 0
    #: 采集过程中的告警
    warnings: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "state": self.state,
            "reason": self.reason,
            "program": self.program,
            "pid": self.pid,
            "source_file": self.source_file,
            "current_file": self.current_file,
            "current_line": self.current_line,
            "current_func": self.current_func,
            "frames": [f.to_json() for f in self.frames],
            "globals": [v.to_json() for v in self.globals],
            "regions": [r.to_json() for r in self.regions],
            "chunks": [c.to_json() for c in self.chunks],
            "heap": [h.to_json() for h in self.heap],
            "heap_freed": [h.to_json() for h in self.heap_freed],
            "heap_total": self.heap_total,
            "lessons": [l.to_json() for l in self.lessons],
            "depth": self.depth,
            "stack_bytes": self.stack_bytes,
            "warnings": self.warnings,
        }

    def dumps(self) -> str:
        return json.dumps(self.to_json(), ensure_ascii=False)


def snapshot_from_json(raw: str) -> Snapshot:
    """从采集端产出的原始 JSON 还原为 Snapshot 对象。"""
    data = json.loads(raw)
    snap = Snapshot()
    for key, value in data.items():
        if key in ("frames", "globals", "regions", "chunks", "heap", "lessons", "warnings"):
            continue
        if hasattr(snap, key):
            setattr(snap, key, value)

    snap.frames = [
        FrameInfo(
            index=f.get("index", i),
            func=f.get("func", "?"),
            file=f.get("file", ""),
            line=f.get("line", 0),
            pc=f.get("pc", 0),
            rsp=f.get("rsp", 0),
            rbp=f.get("rbp", 0),
            low=f.get("low", 0),
            high=f.get("high", 0),
            size=f.get("size", 0),
            vars=[VarInfo(**v) for v in f.get("vars", [])],
        )
        for i, f in enumerate(data.get("frames", []))
    ]
    snap.globals = [VarInfo(**v) for v in data.get("globals", [])]
    snap.regions = [RegionInfo(**r) for r in data.get("regions", [])]
    snap.chunks = [MemChunk(**c) for c in data.get("chunks", [])]
    snap.heap = [HeapBlock(**h) for h in data.get("heap", [])]
    snap.lessons = [Lesson(**l) for l in data.get("lessons", [])]
    snap.warnings = list(data.get("warnings", []))
    return snap


def format_address(addr: Optional[int]) -> str:
    """统一地址格式。

    GDB 给的是 0x00007ff6557b1000，而 MinGW 的 printf("%p") 打的是
    00007FF6EB703000（无 0x 前缀的大写十六进制）。初学者逐字对照时会困惑，
    所以内部统一用 0x + 大写，前端再额外展示 printf 形式。
    """
    if addr is None:
        return "—"
    return "0x%016X" % addr


def format_address_printf(addr: Optional[int]) -> str:
    """MinGW printf("%p") 的输出形式：16 位大写十六进制、无 0x 前缀。"""
    if addr is None:
        return "(nil)"
    return "%016X" % addr
