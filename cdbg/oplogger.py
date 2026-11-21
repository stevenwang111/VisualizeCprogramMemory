"""操作记录器（后端）。

职责很窄：把前端推过来的「用户操作事件」按顺序追加到一个纯文本文件里。

设计约束 —— 它是观测者，绝不能影响调试器本体：
1. 任何写盘异常都吞掉，不向上抛；
2. 文件路径固定在「项目根目录/log」，与启动时的工作目录无关；
3. 文件名用日志启动时刻（毫秒），一次启动对应一个文件；
4. 读写共用一把锁，多线程 HTTP 服务下不会把两行写串。
"""

from __future__ import annotations

import datetime
import os
import threading
from typing import Any, Dict, List, Optional

# cdbg/oplogger.py -> 上一级就是项目根目录（放 cdbg.py 的地方）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, "log")


def _fmt(value: Any) -> str:
    """把字段值拍平成一行日志里能读的字符串。"""
    if value is None:
        return ""
    text = str(value)
    # 换行会让一行日志变成多行，破坏「一行一个事件」的约定
    return text.replace("\r", "\\r").replace("\n", "\\n")


class OpLogger:
    """一次调试会话对应一个实例，一个实例对应一个日志文件。"""

    def __init__(self, base_dir: Optional[str] = None) -> None:
        self.dir = os.path.abspath(base_dir or LOG_DIR)
        try:
            os.makedirs(self.dir, exist_ok=True)
        except Exception:
            # 建目录失败也要让主程序继续跑，最多是没有日志
            pass

        now = datetime.datetime.now()
        stamp = now.strftime("%Y%m%d_%H%M%S") + "_%03d" % (now.microsecond // 1000)
        self.started_at = now
        self.filename = "oplog_%s.log" % stamp
        self.path = os.path.join(self.dir, self.filename)

        self._lock = threading.Lock()
        self._enabled = True
        with self._lock:
            try:
                with open(self.path, "w", encoding="utf-8") as fh:
                    fh.write(
                        "[%s] [SESSION] 日志启动 file=%s\n"
                        % (now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], self.filename)
                    )
            except Exception:
                self._enabled = False

    def write(self, category: str, message: str, fields: Optional[Dict[str, Any]] = None) -> None:
        """追加一条事件。任何异常都在这里消化，不影响调用方。"""
        if not self._enabled:
            return
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        parts = ["[%s]" % ts, "[%s]" % _fmt(category).upper(), _fmt(message)]
        if fields:
            for key, value in fields.items():
                text = _fmt(value)
                if text:
                    parts.append("%s=%s" % (key, text))
        line = " ".join(parts)

        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
            except Exception:
                self._enabled = False

    def read_lines(self, limit: Optional[int] = None) -> List[str]:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except Exception:
                lines = []
        if limit and limit > 0:
            return lines[-limit:]
        return lines

    def info(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "file": self.filename,
            "path": self.path,
            "started_at": self.started_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
