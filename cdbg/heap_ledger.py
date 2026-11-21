"""堆账本解析。

插桩后的被调试程序会把每一次 malloc / free 写进一个 sidecar 文件，
这里负责把它解析成「当前还活着的堆块」列表。

记录格式（每行一条）::

    M <地址> <大小> <文件> <行号>      分配
    F <地址> <文件> <行号>              释放
    R <旧地址> <新地址> <大小> <行号>   重分配
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class HeapEntry:
    """一个堆块。"""

    address: int
    size: int
    file: str = ""
    line: int = 0
    #: 是否已被 free
    freed: bool = False
    #: 释放它的源码位置
    freed_at: str = ""

    @property
    def label(self) -> str:
        if self.freed:
            return "%d 字节 @ %s:%d（已释放）" % (self.size, self.file, self.line)
        return "%d 字节 @ %s:%d" % (self.size, self.file, self.line)

    def to_json(self) -> dict:
        return {
            "address": self.address,
            "size": self.size,
            "file": self.file,
            "line": self.line,
            "freed": self.freed,
            "freed_at": self.freed_at,
            "label": self.label,
        }


@dataclass
class HeapLedger:
    """堆账本。"""

    blocks: List[HeapEntry] = field(default_factory=list)
    #: 解析过程中的异常
    errors: List[str] = field(default_factory=list)

    @property
    def live_blocks(self) -> List[HeapEntry]:
        return [b for b in self.blocks if not b.freed]

    @property
    def total_bytes(self) -> int:
        return sum(b.size for b in self.live_blocks)

    def to_json(self) -> dict:
        return {
            "blocks": [b.to_json() for b in self.blocks],
            "live": len(self.live_blocks),
            "total_bytes": self.total_bytes,
            "errors": self.errors,
        }


def _addr(text: str) -> int:
    """解析地址。

    地址是用 %p 打出来的 —— 十六进制、大写、无 0x 前缀
    （和 printf("%p") 的输出格式一致）。
    所以这里必须按十六进制解析：像 "7432B0" 这种含字母的，
    当十进制解析会直接抛 ValueError。
    """
    text = text.strip()
    if text.lower().startswith("0x"):
        return int(text, 16)
    return int(text, 16)


def _num(text: str) -> int:
    """解析十进制数字（大小、行号等）。"""
    return int(text.strip(), 10)


def parse(path: str) -> HeapLedger:
    """解析堆账本文件。"""
    ledger = HeapLedger()
    if not path or not os.path.isfile(path):
        return ledger

    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        ledger.errors.append("读取堆账本失败: %s" % exc)
        return ledger

    by_address: Dict[int, HeapEntry] = {}

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        kind = parts[0]

        try:
            if kind == "M" and len(parts) >= 3:
                addr = _addr(parts[1])
                size = _num(parts[2])
                file = parts[3] if len(parts) > 3 else ""
                line_no = _num(parts[4]) if len(parts) > 4 else 0
                entry = HeapEntry(address=addr, size=size, file=file, line=line_no)
                by_address[addr] = entry
                ledger.blocks.append(entry)

            elif kind == "F" and len(parts) >= 2:
                addr = _addr(parts[1])
                entry = by_address.get(addr)
                if entry is None:
                    entry = HeapEntry(address=addr, size=0)
                    by_address[addr] = entry
                    ledger.blocks.append(entry)
                entry.freed = True
                entry.freed_at = "%s:%s" % (
                    parts[2] if len(parts) > 2 else "",
                    parts[3] if len(parts) > 3 else "",
                )

            elif kind == "R" and len(parts) >= 4:
                old = _addr(parts[1])
                new = _addr(parts[2])
                size = _num(parts[3])
                file = parts[4] if len(parts) > 4 else ""
                line_no = _num(parts[5]) if len(parts) > 5 else 0
                old_entry = by_address.get(old)
                if old_entry is not None:
                    old_entry.freed = True
                    old_entry.freed_at = "realloc"
                entry = HeapEntry(address=new, size=size, file=file, line=line_no)
                by_address[new] = entry
                ledger.blocks.append(entry)

        except (ValueError, IndexError):
            ledger.errors.append("无法解析的一行: %s" % line[:80])

    return ledger
