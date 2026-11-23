"""调试会话编排。

把「编译 → 起 GDB → 执行控制 → 采集 → 广播快照」串成一条线，
并对外提供线程安全的命令接口给 Web 层调用。
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .collector import Collector
from .gdb_session import GdbError, GdbSession
from .launcher import BuildError, BuildResult, compile_source
from .snapshot import Snapshot


class DebugSession:
    """一次调试会话。"""

    def __init__(
        self,
        source: str,
        workdir: Optional[str] = None,
        instrument: bool = True,
        gdb_path: Optional[str] = None,
    ):
        self.source = os.path.abspath(source)
        self.workdir = workdir
        self.instrument = instrument
        self.gdb_path = gdb_path

        self.build: Optional[BuildResult] = None
        self.gdb: Optional[GdbSession] = None
        self.collector = Collector(program=self.source)

        self.snapshot: Optional[Snapshot] = None
        self.source_lines: List[str] = []
        self.original_source_path = self.source
        self.instrument_notes: List[str] = []
        self.heap_log_path: Optional[str] = None
        #: 插桩造成的行号偏移
        self.line_offset: int = 0
        #: 编译错误（带行号），界面据此在源码上标红
        self.build_errors: List[Dict[str, Any]] = []
        self.error_text: str = ""

        self._lock = threading.RLock()
        #: 「会等程序停下」的命令用这把锁串行化。
        #:
        #: 刻意和 `_lock` 分开：`_lock` 护着状态字段，必须秒进秒出；
        #: 单步 / 跳出这类命令要等好几秒，占着 `_lock` 的话，
        #: 「停止执行」「重启」和 /api/state 会一起被堵死 ——
        #: 那正是「程序卡住、界面也一起卡住」的来源。
        self._exec_lock = threading.Lock()
        #: 中止信号：置位后，所有正在等 *stopped 的命令立刻放弃等待。
        #:
        #: 「停止执行」「重启」「清空界面」在动手之前先把它置位，
        #: 这样它们不用等前一条命令自己超时（最长十几秒）。
        #: 每条新命令开始时清掉。
        self._abort_wait = threading.Event()
        self._listeners: List[Callable[[Snapshot], None]] = []
        self._running = False
        self._error: Optional[str] = None
        self._pending_break: Optional[Dict[str, Any]] = None
        #: 会话阶段："not-started"（未执行）| "running" | "stopped" | "exited"
        self.state_name: str = "not-started"
        #: 重启前记下的快照序号，用来等一份更新的快照
        self._restart_seq: Optional[int] = None
        #: 后台监视线程（监视程序什么时候停下来）
        self._monitor: Optional[threading.Thread] = None
        self._stop_monitor = threading.Event()
        #: 采集锁 —— 命令线程和监视线程都会采集快照
        self._collect_lock = threading.RLock()
        #: 是否处于「命令已异步发出、等后台收尾」的状态
        self._async_wait = False
        #: 事件归属锁 —— 命令线程和监视线程靠它保证不同时等 *stopped
        self._event_lock = threading.RLock()
        #: 操作日志（web 服务起来后注入）。为 None 时所有记录都跳过，
        #: 观测层永远不参与业务判断。
        self.oplog = None
        #: 被调试程序的 PID（从快照里抄的）。「长按停止 = taskkill /F」要用它。
        #: 每次新起一个 inferior 都要先清空，否则可能对着上一个进程的
        #: 旧 PID 开火 —— Windows 会复用 PID，那枪就打偏了。
        self._target_pid: Optional[int] = None
        #: 被调试程序的输出（PTY / 控制台）。按 offset 增量读取，
        #: 程序退出后也保留最后的内容。
        self._console_log: str = ""
        self._console_lock = threading.Lock()

    def _log(self, category: str, message: str, fields: Optional[Dict[str, Any]] = None) -> None:
        """往操作日志里记一笔。任何异常都吞掉 —— 它只是观测者。"""
        logger = getattr(self, "oplog", None)
        if logger is None:
            return
        try:
            logger.write(category, message, fields or {})
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------

    def _find_companions(self, workdir: str, source_text: str) -> List[str]:
        """找出「定义」了本文件缺失符号的同目录 .c 文件。"""
        from .launcher import collect_undefined_symbols, find_symbol_definers

        try:
            target = os.path.join(workdir, "_companion_probe.c")
            with open(target, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(source_text)
            undefined = collect_undefined_symbols(self.source, workdir, target)
            if not undefined:
                return []
            definers, _static = find_symbol_definers(self.source, workdir, undefined)
            return list(definers.keys())
        except Exception:
            return []

    def start(self) -> bool:
        """编译并启动 GDB，停在 main。"""
        from . import instrument
        from .launcher import _read_source_text, make_workspace

        workdir = self.workdir or make_workspace(self.source)
        with self._console_lock:
            self._console_log = ""
        self.original_source_path = self.source
        self.source_lines = _read_lines(self.source)

        # ---- 插桩（可选）----
        original_text = _read_source_text(self.source)
        heap_log_path = os.path.join(workdir, "cdbg_heap.log")
        text, extra_sources, notes, line_offset = instrument.prepare_build(
            self.source,
            workdir,
            original_text,
            enable=self.instrument,
            heap_log_path=heap_log_path,
        )
        self.instrument_notes = notes
        # 插桩会让工作副本的行号整体下移，采集到的行号必须减回去，
        # 否则界面上高亮的行和用户看到的原始源码对不上。
        self.line_offset = line_offset

        # ---- 编译 ----
        # 插桩只是锦上添花：万一插桩导致编译失败，先降级为不插桩再试一次。
        # 最终仍失败时，把出错位置记下来（而不是只丢一屏日志），
        # 让界面照常打开、并在源码上标红出错行 —— 对初学者来说，
        # 「我错在哪一行」远比一屏编译日志有用。
        #
        # error_offset 记录「最后一次失败的编译用的是哪份文本」的行号偏移：
        # 插桩版要减掉偏移才能对上原始源码，不插桩版本来就是原始行号。
        build_error = None
        error_offset = self.line_offset

        try:
            self.build = compile_source(
                self.source,
                workdir=workdir,
                extra_sources=extra_sources,
                override_text=text,
            )
        except BuildError as exc:
            build_error = exc
            error_offset = self.line_offset

        # 降级一：关掉插桩再试。
        # 只有「带插桩失败、不带插桩成功」才说明是插桩引起的；
        # 两次都失败的话，那是源码本身有问题，不能赖到插桩头上。
        if build_error is not None and extra_sources:
            try:
                self.build = compile_source(
                    self.source,
                    workdir=workdir,
                    extra_sources=[],
                    override_text=original_text,
                )
                build_error = None
                self.line_offset = 0
                self.instrument = False
                self.instrument_notes.append(
                    "插桩后编译失败，已自动关闭堆跟踪（源码本身没问题）"
                )
            except BuildError as exc:
                build_error = exc
                error_offset = 0

        # 降级二：符号在同目录的别的 .c 文件里？把那些文件一起编译。
        # 这对应教材里「多文件工程 / extern 声明与定义」那一节。
        if build_error is not None:
            companions = self._find_companions(workdir, original_text)
            if companions:
                names = "、".join(os.path.basename(c) for c in companions)
                try:
                    self.build = compile_source(
                        self.source,
                        workdir=workdir,
                        extra_sources=extra_sources + companions,
                        override_text=original_text,
                    )
                    build_error = None
                    self.line_offset = 0
                    self.instrument_notes.append(
                        "检测到符号定义在同目录的 %s 里，已自动一起编译" % names
                    )
                except BuildError as exc:
                    build_error = exc
                    error_offset = 0

        if build_error is not None:
            self.build_errors = [
                {
                    "file": d.get("file", ""),
                    "line": d.get("line", 0),
                    "col": d.get("col", 0),
                    "level": d.get("level", "error"),
                    "message": d.get("message", ""),
                }
                for d in (build_error.diagnostics or [])
            ]
            # 错误行号对应的是「最后一次编译所用的那份文本」，
            # 要减掉那份文本的插桩偏移才对得上用户看到的原始源码。
            if error_offset:
                for d in self.build_errors:
                    if d["line"]:
                        d["line"] = max(0, d["line"] - error_offset)
            self.error_text = str(build_error)
            return False

        probe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gdb_probe.py")
        snap_path = os.path.join(workdir, "cdbg_snapshot.json")
        self.heap_log_path = heap_log_path
        try:
            if os.path.exists(self.heap_log_path):
                os.remove(self.heap_log_path)
        except OSError:
            pass
        self.collector.heap_log_path = self.heap_log_path
        self.collector.line_offset = self.line_offset

        self.gdb = GdbSession(
            exe_path=self.build.exe_path,
            probe_path=probe,
            snapshot_path=snap_path,
            gdb_path=self.gdb_path,
            cwd=workdir,
            source_path=self.build.source_copy,
        )

        # GDB 子进程继承父进程的环境变量 —— 探针靠它找到堆账本。
        # 必须在 GDB 启动 **之前** 设置，否则子进程拿不到。
        # （shim 用的是编译期常量，两边指向同一个文件。）
        os.environ["CDBG_HEAP_LOG"] = self.heap_log_path

        self.gdb.start()

        # 堆账本路径已经由插桩模块写死进编译产物（CDBG_HEAP_LOG_PATH）。
        # 刻意**不**用 GDB 的 `set environment`：它在 Windows 原生调试下
        # 不可靠，实测传过去的值会带上多余的引号，导致 fopen 失败。
        if self.instrument:
            self.instrument_notes.append(
                "堆账本路径：%s" % self.heap_log_path
            )

        # 注意：这里**不运行**程序。
        #
        # 加载源码 ≠ 执行源码。Dev-C++ 打开一个 .c 文件时程序是不动的，
        # 只有你按下「调试」它才开始跑。cdbg 保持同样的语义：
        # 这一步只把符号加载进来，让用户能先看源码、看全局变量布局、
        # 设好断点，再决定要不要跑。
        self._running = False
        self.state_name = "not-started"
        self._publish_idle_snapshot()
        self._start_monitor()
        return True

    def _publish_idle_snapshot(self) -> None:
        """发布一份「尚未执行」状态的快照。

        程序还没跑，所以没有栈帧、没有内存字节，但仍然能给出：
        源码位置、全局变量的地址（符号表里就有）、可执行文件的段布局。
        这些信息对初学者理解「变量住在哪」同样有价值。

        实现上直接让探针采一次 —— 探针本来就区分得了「有帧」和「无帧」，
        没必要在 Python 侧另写一套。
        """
        raw: Dict[str, Any] = {
            "state": "not-started",
            "reason": "not-started",
            "pid": None,
            "program": self.build.exe_path if self.build else "",
            "current_file": "",
            "current_line": 0,
            "current_func": "",
            "frames": [],
            "globals": [],
            "chunks": [],
            "regions": [],
            "depth": 0,
            "stack_bytes": 0,
            "warnings": [],
        }

        # 让探针采一次：全局变量的地址与段布局都来自符号表，
        # 不需要进程在跑。
        if self.gdb is not None:
            try:
                self.gdb.send('-interpreter-exec console "cdbg-collect"', timeout=8)
                data = self.gdb.read_snapshot(timeout=2.0)
            except GdbError:
                data = None
            if data:
                raw["globals"] = data.get("globals", [])
                raw["regions"] = data.get("regions", [])
            # 这份快照已经消费掉了，别让后面的 wait_new_snapshot 把它当新的
            self.gdb.sync_seq()

        snap = self.collector.build(raw)
        snap.source_file = self.original_source_path
        with self._lock:
            self.snapshot = snap
        self._broadcast(snap)

    def shutdown(self) -> None:
        self._running = False
        self._stop_monitor_thread()
        if self.gdb is not None:
            self.gdb.close()
            self.gdb = None

    # ------------------------------------------------------------------
    # 执行控制
    # ------------------------------------------------------------------

    def command(self, cmd: str, **kwargs) -> Dict[str, Any]:
        """执行一条调试命令。

        命令名与 Dev-C++ 保持一致：
          debug    调试（无断点时停在入口，有断点时跑到下一个断点）
          next     单步执行（不进入函数）
          step     单步进入
          finish   跳出函数
          goto     运行到光标行
          continue 继续运行
          stop     停止执行（短按；长按走 force-kill）
          break    设置/取消断点
          restart  重新开始
          clear    清空界面（扔掉这次调试的画面，源码和断点留着）
          force-kill  taskkill /F 掉被调试程序（「停止」长按 1 秒用）

        锁的规矩（「卡住」问题的根子，别改回去）：

        - `_lock` 只护状态字段，持有时间必须是毫秒级；
        - `_exec_lock` 只串行化「会等程序停下」的命令；
        - **等程序那几秒不能抱着上面任何一把锁**。

        最后一条是关键。以前整条命令（含那个 8 秒的等待）都裹在
        `with self._lock` 里，于是在 scanf 上按「跳出函数」时，
        停止按钮、重启按钮、连 /api/state 全被堵住 —— 日志里那次
        11 秒的 finish 就是这么来的。
        """
        if self.gdb is None:
            return {"ok": False, "message": "调试会话未启动"}

        # 四条「救命」命令：随时可用，不排队。
        # 它们要能打断正在等待的那条命令，所以放在 _exec_lock 之前。
        if cmd == "stop":
            return self._stop_execution()
        if cmd == "force-kill":
            return self._force_kill()
        if cmd == "restart":
            return self._restart_command()
        if cmd == "clear":
            return self._clear_view()

        if cmd == "break":
            with self._exec_lock:
                return self._toggle_breakpoint(
                    int(kwargs.get("line", 0) or 0),
                    bool(kwargs.get("on", True)),
                )

        # 继续运行可能长时间不停（比如程序里没有更多断点了），
        # 所以给个较短的等待时间，避免界面一直等。
        wait = 1.0 if cmd == "continue" else 8.0

        try:
            with self._exec_lock:
                # 新命令开始 —— 上一条「停止」留下的中止信号作废
                self._abort_wait.clear()

                # 发 run 之前得先把上一次还在跑的程序停掉。
                # 这一步最长一两秒，刻意放在锁外面做。
                if cmd == "debug":
                    self._stop_previous()

                early: Optional[Dict[str, Any]] = None
                with self._lock, self._event_lock:
                    # 清掉上一次命令残留的事件，否则会把旧的 *stopped 当成这次的
                    self.gdb.drain_events()
                    # 命令线程要自己收尾，监视线程别跟它抢事件
                    self._async_wait = False
                    if cmd == "debug":
                        self._start_debugging()
                    elif cmd == "run":
                        self.gdb.run()
                    elif cmd == "next":
                        self.gdb.step_over()
                    elif cmd == "step":
                        self.gdb.step_into()
                    elif cmd == "finish":
                        # 跳不出去的函数（scanf 之流）先拦下来 ——
                        # 否则 GDB 会一直等到它返回，而它多半在等人输入。
                        early = self._finish_guard()
                        if early is None:
                            self.gdb.step_out()
                    elif cmd == "continue":
                        self.gdb.continue_()
                        # 继续运行可能长时间不停（比如程序在等 scanf 输入），
                        # 所以不等它停下 —— 状态变化由 SSE 异步推给界面。
                        # （Dev-C++ 也是这样：F4 按下就返回，不卡界面。）
                        self._running = True
                        self.state_name = "running"
                        self._async_wait = True
                        self._log("PHASE", "继续运行", {"phase": "running"})
                        # 立刻把「运行中」这个状态推出去 —— 否则界面还是旧的
                        self._broadcast_phase()
                        return {"ok": True, "message": ""}
                    elif cmd == "goto":
                        line = int(kwargs.get("line", 0) or 0)
                        if not line:
                            return {"ok": False, "message": "没有指定目标行"}
                        # 换算成工作副本的坐标（插桩会整体下移行号）
                        self.gdb.run_to_line(line + self.line_offset)
                    else:
                        return {"ok": False, "message": "未知命令: %s" % cmd}

                if early is not None:
                    # 前置检查没过（当前这一帧跳不出去）—— 命令根本没发给 GDB，
                    # 立刻回话，界面不会有任何等待。
                    self._log("CMD", "跳出被拦下", {"msg": early.get("message", "")})
                    return early

                # 等程序停下来。等不到就告诉前端「可能卡在等输入」——
                # 否则提示自己消失，用户完全不知道发生了什么。
                stopped = self._collect_and_broadcast(wait=wait)

                aborted = self._abort_wait.is_set()
                if not stopped and not aborted:
                    # 没等到程序停下：它要么还在慢慢跑，要么卡在 scanf 上等人输入。
                    # 把收尾交给后台监视线程 —— 等它哪天真停下来（断点命中 /
                    # 输入完成），界面还能自己更新，不用再点一次按钮。
                    with self._lock:
                        self._async_wait = True

                result: Dict[str, Any] = {
                    "ok": True,
                    "message": "",
                    "waiting": (not stopped) and (not aborted),
                }
                if not stopped and not aborted:
                    # 等不到 *stopped：程序多半卡在 scanf / getline 上等人输入。
                    # 记一笔，复盘时能立刻区分「命令很慢」和「在等人」。
                    self._log("WAIT", "没等到程序停下（可能在等输入）", {"cmd": cmd})

                # 「运行到光标」：把光标挪到程序**真正停下**的那一行。
                # 光标指在空行 / 声明行上时，GDB 会吸附到下一个有代码的行，
                # 不回报的话界面就会一直显示「光标在 18 行、程序停在 19 行」。
                if cmd == "goto" and stopped:
                    with self._lock:
                        snap = self.snapshot
                    if snap is not None and snap.current_line:
                        result["line"] = snap.current_line
                return result
        except GdbError as exc:
            return {"ok": False, "message": str(exc)}

    # ------------------------------------------------------------------
    # 「救命」命令：停止 / 强制结束 / 重启 / 清空
    # ------------------------------------------------------------------

    def _stop_execution(self) -> Dict[str, Any]:
        """停止执行 —— 短按和长按的「先手」都走这里。

        这条命令必须**随时**能走通，所以它不等 `_exec_lock`：

        1. 先放中止信号，让正在等 *stopped 的那条命令立刻放弃
           （比如停在 scanf 上按「跳出函数」，那条命令会等十几秒）；
        2. 再走 interrupt → kill，几十毫秒就能确认死透。

        这个顺序是「按钮点了没反应、只能关掉重开」的解药：
        以前停止排在命令锁后面，前一条命令不超时，它就一动不动。
        """
        if self.gdb is None:
            return {"ok": False, "message": "调试会话未启动"}

        # 程序早就退出了 / 连跑都没跑过 —— 没有可杀的东西，别白等一轮超时
        if self.state_name == "exited":
            return {"ok": True, "message": "程序已经退出了", "phase": "exited"}
        if self.state_name == "not-started":
            return {"ok": True, "message": "程序还没开始运行", "phase": "not-started"}

        self._abort_wait.set()
        try:
            # kill 是「中断 → 杀掉 → 等退出事件」的同步流程，几十毫秒就能确认。
            # 以前它是发后不理，导致界面上报「已停止执行」而程序还在跑，
            # 接着点调试又起不来。
            dead = self.gdb.kill()
        except GdbError:
            dead = False
        except Exception:
            dead = False

        with self._lock:
            self._running = False
            self.state_name = "stopped"
            self._async_wait = False
            self._restart_seq = None

        # 先把「已停止」推给界面，再收拾残局：
        # 程序卡住时 kill 要花一两秒（Windows 上得先把阻塞的读打断），
        # 先广播能让界面立刻从「卡住了」里出来。
        self._publish_stopped_snapshot()
        self._log("PHASE", "停止执行", {
            "phase": "stopped", "killed": 1 if dead else 0,
        })

        # 中断那一下探针会写一份「停在 ntdll」的快照。
        # 它可能在 kill 返回之后才落盘，所以等文件稳定再同步，
        # 否则下一条命令会立刻读到这份「已退出」的旧快照。
        try:
            self.gdb.sync_settled()
            self.gdb.drain_events()
        except Exception:
            pass

        return {
            "ok": True,
            "message": "已停止执行" if dead else "已发出停止指令，但没等到程序退出",
            "phase": "stopped",
            "killed": bool(dead),
        }

    def _restart_command(self) -> Dict[str, Any]:
        """重启：从头再跑一遍。

        卡死时用户的第一反应就是重启，所以它跟「停止」一样走救命通道：
        先叫停正在等待的那条命令，再 kill + run，然后立刻回话。
        新程序停在 main（或第一个断点）时的快照由监视线程收。

        以前它走通用路径，要等满 8 秒的 *stopped 才回话 ——
        程序一跑起来按钮全灰，用户只能干看着。
        """
        if self.gdb is None:
            return {"ok": False, "message": "调试会话未启动"}

        self._abort_wait.set()
        try:
            self._restart()
        except GdbError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:
            return {"ok": False, "message": "重启失败: %s" % str(exc)[:160]}

        with self._lock:
            self._async_wait = True
        self._broadcast_phase()
        return {"ok": True, "message": "", "phase": "running"}

    def _stop_previous(self) -> None:
        """把上一次还在跑的程序停掉（发新 run 之前必须做）。

        刻意放在状态锁外面：这一步最长要一两秒，占着 `_lock` 的话
        「停止执行」会跟着一起卡。
        """
        if self.gdb is None or not self.gdb.target_running:
            return
        try:
            # 这一步几十毫秒（中断 + kill），失败也不影响后面重试
            self.gdb.kill(timeout=4.0)
            self.gdb.drain_events()
            # 等探针把「被杀掉」那份快照写完，再记序号 —— 否则紧接着的
            # 采集会拿到它，界面上就会「刚点调试就显示程序已退出」。
            self.gdb.sync_settled()
        except Exception:
            pass

    def _finish_guard(self) -> Optional[Dict[str, Any]]:
        """「跳出函数」的前置检查：跳不出去的时候直接拦下来。

        为什么要有这道检查：`-exec-finish` 的语义是「一直跑到当前函数
        返回」。停在 scanf 这类系统函数里时，它得等你输入数据才会返回 ——
        于是这条命令会一直等下去（实测 11 秒才超时），这期间界面就像
        整个卡死了。而用户真正想要的通常是「回到我自己的代码」，
        那是「单步执行」的事。

        返回 None 表示可以跳；否则返回一条「不跳」的回话。
        """
        with self._lock:
            snap = self.snapshot
        if snap is None or snap.state in ("not-started", "exited"):
            return {
                "ok": False,
                "message": "程序现在没有停在任何地方，没法「跳出函数」—— 先按「调试」让它跑起来",
            }
        if not snap.frames:
            return {"ok": False, "message": "拿不到当前调用栈，没法「跳出函数」"}

        frame = snap.frames[0]
        name = frame.func or "?"
        if name == "main":
            return {
                "ok": False,
                "message": "已经在 main 函数里了，没有上层函数可以跳出 —— 想跑完请按「继续」",
            }
        if not self._is_user_frame(frame.file):
            return {
                "ok": False,
                "message": (
                    "当前停在 %s 这类系统函数里，「跳出」只会等到它返回"
                    "（经常就是等输入）—— 先用「单步执行」回到自己的代码，"
                    "或者先在程序窗口里把数据输进去" % name
                ),
            }
        return None

    def _is_user_frame(self, path: str) -> bool:
        """判断一个栈帧是不是「用户自己的代码」。

        系统库（ucrt / mingw 的源码）没有「跳出到上层用户代码」这回事，
        而且它们常常在等 I/O，跳出去就是干等。它们的样子很好认：
        路径是光秃秃的文件名（GDB 从 DLL 的调试信息里抠出来的），
        或者落在编译器 / 系统目录里。
        """
        if not path:
            return False
        # 自己那份源码：GDB 有时报绝对路径，有时只报文件名
        base = os.path.normcase(os.path.basename(path))
        if base == os.path.normcase(os.path.basename(self.original_source_path)):
            return True
        # 同目录下的多文件工程（另一份 .c/.h）也算用户代码
        if not os.path.isabs(path):
            return False
        try:
            user_dir = os.path.dirname(os.path.abspath(self.original_source_path))
            if os.path.normcase(os.path.dirname(os.path.abspath(path))) == os.path.normcase(user_dir):
                return True
        except Exception:
            pass
        lower = path.replace("\\", "/").lower()
        for marker in ("/mingw", "/msys", "/ucrt", "/windows/", "/lib/gcc", "/include/"):
            if marker in lower:
                return False
        return True

    def _start_debugging(self) -> None:
        """开始调试。

        和 Dev-C++ 一致：
        - 用户一个断点都没设 → 停在程序入口（GDB 的 start）
        - 设了断点 → 直接跑，停在第一个命中的断点

        程序之前跑过并退出了也没关系 —— 这里相当于重新启动一次。

        但**上一个程序还在跑**时必须先停掉：否则 `-exec-run` 只会得到
        "The program is already running"，界面看着像点了没反应。
        这一步由 `_stop_previous()` 在锁外面做（见 command 里 cmd == "debug"）。
        """
        if self.gdb.has_user_breakpoint():
            self.gdb.run()
        else:
            self.gdb.start_program()
        # 新起的 inferior 会有新 PID，旧的立刻作废
        self._target_pid = None
        self._running = True
        self.state_name = "running"
        self._log("PHASE", "开始调试", {
            "phase": "running",
            "mode": "run" if self.gdb.has_user_breakpoint() else "start",
        })

    def _toggle_breakpoint(self, line: int, on: bool) -> Dict[str, Any]:
        """设置或取消一个源码行上的断点。

        关键：行号要在「用户看到的源码」和「编译产物」之间换算。
        插桩会在源码前面插入几行，工作副本的行号因此整体下移。
        设断点要用工作副本的坐标，否则会停在错误的地方。
        """
        if not line:
            return {"ok": False, "message": "没有指定行号"}
        if on:
            num = self.gdb.set_line_breakpoint(line + self.line_offset)
            if num is None:
                self._log("BREAK", "设置断点失败", {"line": line})
                return {"ok": False, "message": "断点设置失败: 第 %d 行" % line}
            # GDB 会把断点吸附到「这一行之后第一个有代码的行」：
            # 空行、纯声明行（`int b;`）、`static int c=0;` 都会往后挪。
            # 把真实落点报给界面，让标记落在程序真正会停的地方。
            actual = self.gdb.last_bkpt_line
            if actual:
                actual -= self.line_offset
            self._log("BREAK", "设置断点", {
                "line": line, "actual": actual or line, "gdb": num,
            })
            if actual and actual != line:
                return {
                    "ok": True,
                    "message": "第 %d 行没有可执行代码，断点落在第 %d 行" % (line, actual),
                    "line": actual,
                }
            return {"ok": True, "message": "已在第 %d 行设置断点" % line, "line": line}
        ok = self.gdb.clear_line_breakpoint(line + self.line_offset)
        if not ok:
            self._log("BREAK", "取消断点失败", {"line": line})
            return {"ok": False, "message": "该行没有断点"}
        self._log("BREAK", "取消断点", {"line": line})
        return {"ok": True, "message": "已取消第 %d 行的断点" % line}

    def _broadcast_phase(self) -> None:
        """只推送状态变化（不动快照内容）。

        程序跑起来的时候界面需要立刻知道「运行中」——
        但这时候还没有新的快照可推（程序还没停），所以沿用旧快照，
        只把会话阶段更新一下。
        """
        with self._lock:
            snap = self.snapshot
        if snap is None:
            return
        self._broadcast(snap)

    def _publish_stopped_snapshot(self) -> None:
        """程序被我们主动停掉时，发布一份「已停止」的快照。

        杀掉程序不会产生 *stopped 事件，所以不能靠等事件拿快照。
        这里沿用上一次采集到的内容，只把状态改成 stopped ——
        变量、内存那些还是最后那次的值，对用户来说正合适：
        「程序停在这里，最后看到的是这些」。
        """
        with self._lock:
            snap = self.snapshot
        if snap is None:
            return
        snap.state = "stopped"
        snap.reason = "killed"
        with self._lock:
            self.snapshot = snap
        self._broadcast(snap)

    # ------------------------------------------------------------------
    # 界面清空 / 强制结束
    # ------------------------------------------------------------------

    def _clear_view(self) -> Dict[str, Any]:
        """清空界面：把这一次调试「看到的一切」扔掉，程序本体和断点都留着。

        为什么要在会话里做，而不是前端把 DOM 擦一遍：
        前端的画面是 `/api/state` + SSE 的投影。只擦 DOM 的话，下一条命令
        的 `fetchState`、或者刷一下页面，旧快照就又回来了 —— 那才是「假的
        清空」。这里把 `snapshot` 置空、阶段退回 `not-started`，并广播一份
        `null`，前端据此把时间轴、变量、内存视图一起清干净，刷新也不会复活。

        如果程序还在跑，先杀掉再清：否则会出现「界面空了、程序在后台继续跑」，
        用户完全失去控制，只能去任务管理器收尸。

        断点刻意**不清**：它属于「源码上的设置」，不随一次运行消失。
        """
        if self.gdb is None:
            return {"ok": False, "message": "调试会话未启动"}

        # 先叫停正在等待的那条命令，免得它等满超时才回来
        self._abort_wait.set()

        killed = False
        if self.gdb.target_running or self.state_name in ("running", "stopped"):
            # 和「停止执行」走同一条路：中断 → kill → 等确认死透
            try:
                killed = self.gdb.kill()
                self.gdb.drain_events()
                # kill 会让探针再写一份快照，先等它落盘再扔，免得它偷偷跑回来
                self.gdb.sync_settled()
            except Exception:
                killed = False

        with self._lock:
            self._async_wait = False
            self._running = False
            self._restart_seq = None
            self._target_pid = None
            self.state_name = "not-started"
            self.snapshot = None
        self._log("PHASE", "清空界面", {"killed": 1 if killed else 0})
        # 广播 null：SSE 那一头收到 null 就知道「现在什么都没有」，
        # 前端会连同时间轴一起清空。
        self._broadcast(None)
        return {
            "ok": True,
            "message": "已清空界面" + ("（程序已停止）" if killed else ""),
            "cleared": True,
            "phase": "not-started",
        }

    def _force_kill(self) -> Dict[str, Any]:
        """绕开 GDB，从外部强制结束被调试程序。

        给「停止执行」的长按用。正常停止是「interrupt → console kill」，
        几十毫秒就能确认；这条路只在 **GDB 自己卡死**时才有用：
        程序卡在某个系统调用里、`*stopped` 事件迟迟不来、console kill 回
        error，这时候只有从外面把进程砍掉，GDB 才会跟着醒过来。

        只杀 inferior，不杀 GDB —— GDB 活下来，符号表和断点都还在，
        用户可以直接接着按「调试」重来。

        拿不到 PID 时（程序刚开始跑、还没停下来采过一次快照）退回
        正规的 interrupt → kill：长按的语义是「一定要把它弄死」，
        不是「必须用 taskkill」。
        """
        if self.gdb is None:
            return {"ok": False, "message": "调试会话未启动"}

        # 这条命令同样要能打断别的等待
        self._abort_wait.set()

        pid = self._target_pid
        if not pid:
            return self._stop_execution()

        killed = _terminate_pid(pid)
        # 进程被外部干掉时 GDB 也会收到退出事件，收一下，别留给下一条命令
        try:
            self.gdb.drain_events()
            self.gdb.sync_settled()
        except Exception:
            pass
        with self._lock:
            self._running = False
            self.state_name = "stopped"
            self._async_wait = False
            self._target_pid = None
        self._publish_stopped_snapshot()
        self._log("PHASE", "强制结束程序", {
            "pid": pid, "killed": 1 if killed else 0,
        })
        if not killed:
            return {
                "ok": False,
                "message": "强制结束失败：进程 %d 可能已经不在了" % pid,
            }
        return {
            "ok": True,
            "message": "已强制结束程序 (PID %d)" % pid,
            "phase": "stopped",
            "killed": True,
        }

    def _start_monitor(self) -> None:
        """后台监视程序状态。

        「继续运行」不能同步等待 —— 程序可能跑很久，也可能停在
        scanf 上等人输入（这时永远不会停）。所以改成后台盯着：
        程序一停下来（断点、单步、退出）就把新快照推给界面。

        这跟 Dev-C++ 的做法一致：F4 按下就返回，界面异步更新。

        关键：监视线程**只在程序是"异步放出去"的**时候才消费事件。
        否则它会跟命令线程抢 *stopped 事件 —— 命令线程抢不到，
        就只能干等到超时（实测每个文件多等 8 秒）。
        """
        if self._monitor is not None and self._monitor.is_alive():
            return

        def loop():
            while not self._stop_monitor.is_set():
                if not self._async_wait:
                    self._stop_monitor.wait(0.05)
                    continue
                # 等命令线程把事件处理完再接手 —— 两边同时等
                # *stopped 的话，命令线程会抢不到，白等满超时。
                with self._event_lock:
                    if not self._async_wait:
                        continue
                    try:
                        event = self.gdb.wait_event("*stopped", timeout=0.2)
                    except Exception:
                        break
                    if event is None:
                        continue
                    self._async_wait = False
                # 程序停了 —— 采集并广播。
                # abortable=False：监视线程是异步收尾的所有者，
                # 不能被一条早已过去的「停止」信号误伤（重启之后
                # 新程序的快照就靠它收）。
                try:
                    self._collect_and_broadcast(wait=0.1, abortable=False)
                except Exception:
                    pass
                if self.state_name == "exited":
                    self._running = False

        self._stop_monitor.clear()
        self._monitor = threading.Thread(target=loop, daemon=True)
        self._monitor.start()

    def _stop_monitor_thread(self) -> None:
        self._stop_monitor.set()
        if self._monitor is not None:
            # 只等一小会儿 —— 监视线程最多 0.3 秒一轮，
            # 但没必要为它多花时间（会话都要关了）。
            self._monitor.join(timeout=0.5)
            self._monitor = None

    def _restart(self) -> None:
        """重新跑一遍当前程序。

        关键：**不重启 GDB 进程**。Dev-C++ 也是这么做的 —— 重启只是
        kill 掉当前程序再 run 一次，GDB 本身和它的符号表都留着。

        之前这里的写法（先 -exec-run 再设 main 断点）有两个问题：
        1. -exec-run 是异步命令，用同步的 send() 等它会等到超时
        2. 每重启一次就多设一个 main 断点，断点越积越多
        """
        if self.gdb is None:
            return

        # 先把当前程序干掉（如果还在跑）。
        # kill 是同步的（中断 → 杀掉 → 等 =thread-group-exited），
        # 确认死了再往下走，否则紧接着发的 run 会撞上
        # "The program is already running"。
        try:
            self.gdb.kill()
        except Exception:
            pass
        self.gdb.drain_events()

        # 记住当前快照序号 —— 重启后要等一份**更新的**快照。
        #
        # 必须在 kill 之后记，而且要等文件稳定（sync_settled）：
        # kill 掉旧程序时探针也会写一份快照，那份内容是空的（没有栈帧），
        # 拿它当结果等于什么都没发生。
        before_seq = self.gdb.sync_settled()

        # 先记住用户的断点行，再清空所有断点 —— 顺序反了就什么都恢复不了
        kept = self.gdb.user_breakpoint_lines()

        # 清掉全部断点（含上次「运行到光标」留下的临时断点），然后恢复用户的
        self.gdb.delete_breakpoints()
        for line in kept:
            self.gdb.set_line_breakpoint(line)
        if self.gdb.has_user_breakpoint():
            self.gdb.run()
        else:
            self.gdb.start_program()
        with self._lock:
            self._target_pid = None
            self._running = True
            self.state_name = "running"
            # 新程序的快照要「比 kill 旧程序时写的那份」更新 ——
            # 这份序号交给监视线程判断（它接管之后的采集）。
            self._restart_seq = before_seq
        self._log("PHASE", "重新开始", {"phase": "running", "breakpoints": len(kept)})

    # ------------------------------------------------------------------
    # 采集与广播
    # ------------------------------------------------------------------

    def _collect_and_broadcast(self, wait: float = 8.0, abortable: bool = True) -> bool:
        """等探针写出新快照，然后广播。

        这个方法会被两个地方调用：命令线程（单步、断点）和后台监视线程
        （程序自己跑起来之后）。所以整体要加锁，避免两边同时采集导致
        快照错乱。

        返回值：这次是否**真的等到了程序停下来**。等不到通常意味着程序
        卡在 scanf / getchar 之类的阻塞调用上 —— 界面据此提示用户去控制台
        看一眼，而不是让提示自己消失。
        """
        if self.gdb is None:
            return False
        with self._collect_lock:
            return self._collect_locked(wait, abortable=abortable)

    def _wait_stopped(self, timeout: float, abortable: bool = True) -> Optional[Dict[str, Any]]:
        """等 *stopped，但随时可以被「停止执行」叫停。

        切成 0.2 秒一片地等：这样 `_abort_wait` 一置位就能醒，
        不用等满整个超时（最长 8 秒）—— 用户点了停止，界面不该还愣在那儿。

        `abortable=False` 给监视线程用：它是「异步收尾」的所有者，
        不能被一条早已过去的「停止」信号误伤（那会导致重启之后
        新程序的快照永远等不到）。
        """
        if self.gdb is None:
            return None
        deadline = time.time() + timeout
        while True:
            if abortable and self._abort_wait.is_set():
                return None
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            event = self.gdb.wait_event("*stopped", timeout=min(0.2, remaining))
            if event is not None:
                return event

    def _wait_new_snapshot(
        self, since: int, timeout: float, abortable: bool = True
    ) -> Optional[Dict[str, Any]]:
        """等一份比 `since` 更新的快照（命令线程的等待可被打断）。"""
        if self.gdb is None:
            return None
        deadline = time.time() + timeout
        while True:
            if abortable and self._abort_wait.is_set():
                return None
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            data = self.gdb.wait_new_snapshot(
                since_seq=since, timeout=min(0.2, remaining)
            )
            if data is not None:
                return data

    def _collect_locked(self, wait: float = 8.0, abortable: bool = True) -> bool:
        """真正干活的版本。调用方必须持有 _collect_lock。"""
        if self.gdb is None:
            return False

        # 重启场景：要等一份比「重启前」更新的快照。
        # 否则可能拿到 kill 旧程序时写的那份（内容是空的）。
        since = self._restart_seq
        self._restart_seq = None

        if since is not None:
            # 重启：直接等一份更新的快照即可。
            # 不要再等 *stopped 事件 —— kill 旧程序时可能已经消耗掉它，
            # 而新程序的停止事件要等程序真的跑起来才来，
            # 那会白等满 8 秒。
            raw = self._wait_new_snapshot(since, timeout=6.0, abortable=abortable)
            if raw is None and abortable and self._abort_wait.is_set():
                return False
            event = None
        else:
            event = self._wait_stopped(wait, abortable=abortable)
            if event is None and abortable and self._abort_wait.is_set():
                # 被「停止执行」/「重启」打断了 —— 这条命令不再采集、不再广播，
                # 收尾交给打断它的那条路（它会发布一份「已停止」的快照）。
                return False
            # 没等到事件时别在这儿多等 3 秒：程序还在跑，探针也不会写新快照。
            raw = self.gdb.read_snapshot(timeout=3.0 if event is not None else 1.0)

        # 兜底：事件说「停住了」，快照却说「程序已退出」——
        # 那读到的一定是「上一条程序被杀掉」时写的旧文件。
        # 再等一份更新的，否则界面会在刚点完调试时误报「程序已退出」。
        if raw is not None and event is not None:
            reason = event.get("reason", "")
            if reason in ("breakpoint-hit", "end-stepping-range",
                          "function-finished", "location-reached",
                          "watchpoint-trigger") and raw.get("state") == "exited":
                fresh = self._wait_new_snapshot(
                    raw.get("seq", 0), timeout=2.0, abortable=abortable
                )
                if fresh is not None:
                    raw = fresh

        if raw is None:
            return False

        snap = self.collector.build(raw)
        # 快照里的文件路径是 ASCII 工作副本的路径，换回用户原始路径
        snap.source_file = self.original_source_path
        for fr in snap.frames:
            if fr.file:
                fr.file = _map_back(fr.file, self.original_source_path, self.build)
        if snap.current_file:
            snap.current_file = _map_back(snap.current_file, self.original_source_path, self.build)

        with self._lock:
            self.snapshot = snap
            if snap.state == "exited":
                self.state_name = "exited"
                self._running = False
                # 进程没了，PID 也就不能拿来开火了
                self._target_pid = None
            else:
                self.state_name = "stopped"
                if snap.pid:
                    self._target_pid = snap.pid
        self._broadcast(snap)
        # 每次停下都记一行 —— 「程序停在哪一行、当时是什么状态」是复盘
        # 一个操作序列时最缺的信息（日志里只有按钮点击是看不出这个的）。
        self._log("SNAP", "采集完成", {
            "line": snap.current_line,
            "func": snap.current_func or "",
            "state": snap.state,
            "seq": snap.seq,
            "frames": len(snap.frames),
        })

        if event:
            reason = event.get("reason", "")
            if reason == "exited-normally" or reason == "exited":
                self._running = False
                self.state_name = "exited"

        # 没等到 *stopped：多半是程序卡在等输入上（GDB 在带程序运行时
        # 不回查询，只能等超时）。这个信号要传给前端。
        return since is not None or event is not None
    def _broadcast(self, snap: Optional[Snapshot]) -> None:
        for listener in list(self._listeners):
            try:
                listener(snap)
            except Exception:
                pass

    def subscribe(self, listener: Callable[[Snapshot], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[Snapshot], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def phase(self) -> str:
        """会话当前阶段，供前端决定按钮可用性。"""
        return self.state_name

    def breakpoint_lines(self) -> List[int]:
        """当前用户设置的断点行（**用户坐标**，即用户看到的源码行号）。"""
        if self.gdb is None:
            return []
        try:
            return [ln - self.line_offset for ln in self.gdb.user_breakpoint_lines()]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 被调试程序的终端
    # ------------------------------------------------------------------

    def console_read(self, offset: int = 0) -> Dict[str, Any]:
        """增量读取程序输出。

        `offset` 是上一次读到的位置（字符数）。返回 `offset` 之后的新内容，
        以及新的 offset —— 前端轮询时不用重复显示旧输出。
        """
        if self.gdb is not None:
            try:
                chunk = self.gdb.console_text()
            except Exception:
                chunk = ""
            if chunk:
                with self._console_lock:
                    self._console_log += chunk
        with self._console_lock:
            text = self._console_log
        try:
            start = max(0, min(int(offset or 0), len(text)))
        except (TypeError, ValueError):
            start = 0
        return {
            "ok": True,
            "text": text[start:],
            "offset": len(text),
            "mode": self.gdb.console_mode if self.gdb is not None else "none",
        }

    def console_write(self, data: str) -> Dict[str, Any]:
        """把用户输入写进程序的 stdin（Linux PTY）。"""
        if self.gdb is None:
            return {"ok": False, "message": "调试会话未启动"}
        if not data:
            return {"ok": True}
        ok = self.gdb.console_write(data)
        if not ok:
            return {"ok": False, "message": "程序没有可写的终端（Windows 请用程序自己的控制台窗口）"}
        return {"ok": True}

    def state(self) -> Dict[str, Any]:
        with self._lock:
            snap = self.snapshot
        bp_lines = self.breakpoint_lines()
        return {
            "ok": True,
            "running": self._running,
            "phase": self.state_name,
            "error": self.error_text or None,
            "build_errors": self.build_errors,
            "instrument_notes": self.instrument_notes,
            "program": self.original_source_path,
            "source": self.source_lines,
            "breakpoints": bp_lines,
            "snapshot": snap.to_json() if snap else None,
        }


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _read_lines(path: str) -> List[str]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except Exception:
        return []
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _map_back(path: str, original: str, build: Optional[BuildResult]) -> str:
    """把 GDB 里的 ASCII 工作副本路径换回用户原始路径。"""
    if not path:
        return path
    if build is None:
        return path
    if os.path.normcase(path) == os.path.normcase(build.source_copy):
        return original
    try:
        if os.path.basename(path) == os.path.basename(build.source_copy):
            return original
    except Exception:
        pass
    return path


def _terminate_pid(pid: int) -> bool:
    """从外面强杀一个进程（不经过 GDB）。返回是否真的干掉了。

    Windows 用 `taskkill /PID <pid> /F`。刻意不用 `subprocess.run(text=True)`：
    taskkill 在中文系统上输出 GBK，换个区域设置就可能解码失败 —— 这里只关心
    退出码，输出一律丢掉。`CREATE_NO_WINDOW` 是为了不闪一个黑框（这个调用
    发生在点按钮的瞬间，闪一下很难看）。
    """
    if not pid:
        return False

    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except OSError:
            return False

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0
