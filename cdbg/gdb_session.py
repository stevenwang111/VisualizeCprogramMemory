"""GDB MI2 会话管理。

职责边界（刻意收窄）：

- **执行控制**归 MI2：断点、运行、单步、继续、结束。
- **数据采集**归 GDB Python 探针（gdb_probe.py）：变量、栈帧、内存字节。

两者通过「一个约定好的 JSON 文件」解耦：探针在程序每次停下来时把快照
原子写入该文件，本模块负责在收到 `*stopped` 事件后等这个文件更新。

这样避免了 MI 与 console 解释器混用带来的输出交错问题。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Tuple


class GdbError(RuntimeError):
    """GDB 返回了 ^error。"""


#: 启动时要设的 GDB 选项，形式是 (变量名, 值)。
#:
#: MI 的写法是 `-gdb-set <变量名> <值>`，**不能**带上 console 命令里的
#: `set` 前缀 —— 写成 `-gdb-set set pagination off` 的话，GDB 会把它理解成
#: 「把名为 set 的变量设成 pagination off」，直接回 ^error。
#: 第一版就栽在这儿：整组设置全部静默失败，其中就包括 new-console，
#: 于是被调试程序一直跟着 GDB 共用一个控制台窗口。
_GDB_SETTINGS = (
    ("pagination", "off"),
    ("confirm", "off"),
    ("print pretty", "off"),
    ("width", "0"),
    ("height", "0"),
    ("disassembly-flavor", "intel"),
    # 让 GDB 在程序运行期间也能读 stdin。
    #
    # 不开这个，程序一 continue，GDB 就卡在 wait_for_inferior 里，
    # 后面发的命令**全部石沉大海**（实测：-exec-interrupt、console kill
    # 发出去 6 秒，stdout 一个字都没有）。表现就是「点停止没反应、
    # 再点调试也起不来，只能重启」。
    ("mi-async", "on"),
)


class GdbSession:
    """管理一个 `gdb --interpreter=mi2` 子进程。"""

    def __init__(
        self,
        exe_path: str,
        probe_path: str,
        snapshot_path: str,
        gdb_path: Optional[str] = None,
        cwd: Optional[str] = None,
        source_path: str = "",
    ):
        self.exe_path = exe_path
        self.probe_path = probe_path
        self.snapshot_path = snapshot_path
        self.gdb_path = gdb_path
        self.cwd = cwd or os.path.dirname(os.path.abspath(exe_path))
        #: 编译时那份源码的路径（设断点要用它，符号表记的是这个名字）
        self.source_path = source_path

        self.proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._events: deque = deque()
        self._console: List[str] = []
        self._program_console: List[str] = []
        self._result: Optional[Dict[str, Any]] = None
        self._closed = False
        self._gdb_ready = False
        self._last_seq = 0
        #: 被调试程序是否已经退出
        self._exited = False
        #: 被调试程序是否正在跑（由 *running / *stopped 事件维护）。
        #:
        #: 必须自己记 —— 程序跑着的时候不能去问 GDB「在跑吗」，
        #: 那等于发一条命令然后等超时。
        self._target_running = False
        self._breakpoints: Dict[str, int] = {}
        #: 用户设置的断点：源码行号 -> GDB 断点号
        self._user_breakpoints: Dict[int, int] = {}
        #: 最近一次 -break-insert 实际落到的行号（工作副本坐标）。
        #:
        #: GDB 不会把断点精确放在你给的那一行上 —— 它吸附到「该行之后第一个
        #: 有机器码的行」。在空行、纯声明行（`int b;`）、`static int c=0;` 上下
        #: 断点，实际落点会往下挪一两行。界面必须显示这个真实落点，否则会出现
        #: 「断点点在 18 行、程序却停在 19 行」的错层。
        self.last_bkpt_line: Optional[int] = None
        #: 断点总数（含自动设置的），用于判断「一个断点都没有」
        self._bp_count = 0
        #: 每启动一次 GDB 递增。旧进程的读线程退出时靠它判断「自己已经过期」，
        #: 否则它会把新会话的 _closed 置成 True（表现为刚重连就卡死）。
        self._proc_gen = 0
        #: new-console 是否设置成功 —— "own" 表示程序有自己的控制台窗口
        self.console_mode = "own" if os.name == "nt" else "none"
        self._new_console = False
        #: 被调试程序的 PTY（Linux 下用它给 scanf / printf 一个真正的终端）
        self._pty_master: Optional[int] = None
        self._pty_slave: Optional[int] = None
        self._pty_stop = threading.Event()
        self._console_io_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动 GDB。

        两套启动方式，和 Dev-C++ 对齐：

        1. **首选**：GDB 自己开一个 console 但立刻隐藏（CREATE_NEW_CONSOLE +
           STARTF_USESHOWWINDOW/SW_HIDE，Dev-C++ 在 Debugger.pas 里就是这么干的），
           再加上 `set new-console on` —— 被调试程序会拿到**自己的**控制台窗口，
           用户只看到那一个窗口，标题是程序路径。
        2. **兜底**：GDB 太老、没有 new-console 这个开关时，把 GDB 的控制台
           放出来，程序共用它 —— 功能照旧，只是窗口是 gdb.exe 那个。

        为什么非要给程序一个独立控制台：GDB 是通过管道和本进程通信的，
        程序的 stdin 若和 MI 命令挤在同一个管道里，scanf 会读到我们的命令、
        或者干脆挂死。
        """
        if os.name == "nt":
            if not self._boot(hide_console=True):
                # new-console 不生效 —— 退回「GDB 控制台可见」的老路子
                self._kill_proc()
                self._boot(hide_console=False)
        else:
            # Linux 不需要 new-console；程序 I/O 由 PTY 承担（见 _setup_pty）
            self._boot(hide_console=False)

    def _boot(self, hide_console: bool) -> bool:
        """启动 GDB 并完成初始化。返回 new-console 是否设置成功。"""
        from .launcher import find_gdb

        gdb = self.gdb_path or find_gdb()

        env = dict(os.environ)
        env["CDBG_OUT"] = self.snapshot_path
        env["CDBG_LOG"] = os.path.join(
            os.path.dirname(self.snapshot_path), "cdbg_probe.log"
        )
        env["PYTHONIOENCODING"] = "utf-8"
        env["LANG"] = "C.UTF-8"

        cmd = [
            gdb,
            "--interpreter=mi2",
            "-q",
            "--nx",
            "-x",
            self.probe_path,
        ]

        creationflags = 0
        startupinfo = None
        if os.name == "nt":
            # Dev-C++ 的做法：GDB 拿一个真正的控制台（CREATE_NEW_CONSOLE），
            # 但用 STARTF_USESHOWWINDOW + SW_HIDE 把那个窗口藏起来。
            # 藏起来不是为了好看 —— 是为了让被调试程序能开自己的控制台，
            # 又不至于多出一个 gdb.exe 的黑窗口。
            creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            if hide_console:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE

        self._proc_gen += 1
        gen = self._proc_gen
        self._close_pty()
        with self._cond:
            self._closed = False
            self._gdb_ready = False
            self._result = None
            self._events.clear()
            self._console.clear()
            self._program_console.clear()

        self.proc = subprocess.Popen(
            cmd,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )

        self._reader = threading.Thread(
            target=self._read_loop, args=(self.proc, gen), daemon=True
        )
        self._reader.start()
        threading.Thread(
            target=self._drain_stderr, args=(self.proc,), daemon=True
        ).start()

        # 清掉上一次运行遗留的快照文件。
        # 工作目录是按源文件路径哈希复用的，不清的话会把上次的旧数据
        # 当成本次的结果读出来（表现为「变量全是空的」这种怪现象）。
        try:
            if os.path.exists(self.snapshot_path):
                os.remove(self.snapshot_path)
        except OSError:
            pass
        self._last_seq = 0

        # 等 GDB 进入 MI 命令循环，再确认它真的开始处理命令
        if not self._wait_ready(timeout=15.0):
            raise GdbError(
                "GDB 启动超时（等待 MI 提示符）。"
                "请检查 gdb 是否可用，或查看 %s" % env.get("CDBG_LOG", "")
            )
        if not self._probe_ready():
            raise GdbError(
                "GDB 未响应 MI 命令。请查看 %s" % env.get("CDBG_LOG", "")
            )

        # 加载目标文件
        self.send("-file-exec-and-symbols %s" % self._quote(self.exe_path))

        # 关掉一些会打断自动化的设置。
        # 注意：出错不再静默吞掉 —— 第一版就是因为吞了错误，
        # 才让「new-console 没生效」这件事一直没被发现。
        for name, value in _GDB_SETTINGS:
            self._gdb_set(name, value)

        # 让被调试程序开自己的控制台窗口 —— 这样它的 printf 输出到
        # 那个窗口、scanf 从那个窗口读键盘，跟 Dev-C++ 的行为一致。
        # 不开这个的话，程序会跟 MI 管道抢 stdin，scanf 必然挂死。
        #
        # TODO(退出后保留控制台)：程序一退出，这个新控制台就跟着销毁，
        #   学生看不到最后几行 printf。Dev-C++ 跑程序时会补上
        #       --------------------------------
        #       Process exited after 8.978 seconds with return value 0
        #       请按任意键继续. . .
        #   要做就跟 Dev-C++ 一样，另外挂一个 AttachConsole 到被调试进程的
        #   保活进程守着那个控制台，等程序退出后再打印这行收尾提示。
        #   现在先保持和 Dev-C++「调试」时一致（窗口随程序一起消失），
        #   等主要功能稳定再加。
        # Linux：给 inferior 一个 PTY，让 printf/scanf 正常工作
        if os.name != "nt":
            self._setup_pty()
            self.console_mode = "pty" if self._pty_master is not None else "none"
            return True

        own_console = self._gdb_set("new-console", "on")
        self.console_mode = "own" if own_console else "shared"
        return own_console

    # ------------------------------------------------------------------
    # PTY：给被调试程序一个真正的终端（Linux）
    # ------------------------------------------------------------------

    def _setup_pty(self) -> bool:
        """创建 PTY 并把它设为 inferior 的终端。

        Windows 用 new-console 给程序开独立控制台；Linux 没有这个概念，
        标准做法是 `pty.openpty()` + `set inferior-tty`。这样：

        - `printf` 的输出能被后台线程读进 console 缓冲，前端看得到；
        - `scanf` 从 PTY 读，不会跟 GDB 的 MI 管道抢 stdin。
        """
        if os.name == "nt":
            return False
        try:
            import pty
        except Exception:
            self._note_console("(cdbg) 当前平台没有 pty 模块，程序输出将不可见\n")
            return False

        try:
            master, slave = pty.openpty()
        except Exception as exc:
            self._note_console("(cdbg) 创建 PTY 失败：%s\n" % exc)
            return False

        name = os.ttyname(slave)
        result = self.send(
            '-interpreter-exec console "set inferior-tty %s"' % name,
            timeout=3.0, raise_on_timeout=False,
        )
        if not result or result.get("kind") == "error":
            for fd in (master, slave):
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._note_console("(cdbg) 设置 inferior-tty 失败，程序输出将不可见\n")
            return False

        self._pty_master = master
        self._pty_slave = slave
        self._pty_stop.clear()
        threading.Thread(
            target=self._pty_read_loop, args=(master,), daemon=True
        ).start()
        self.console_mode = "pty"
        return True

    def _pty_read_loop(self, master: int) -> None:
        """后台读 PTY，把程序输出放进 console 缓冲。"""
        import select

        while not self._pty_stop.is_set():
            try:
                ready, _, _ = select.select([master], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data = os.read(master, 65536)
            except (OSError, ValueError):
                break
            if not data:
                break
            text = data.decode("utf-8", errors="replace")
            with self._cond:
                self._program_console.append(text)
                self._cond.notify_all()

    def _close_pty(self) -> None:
        """关闭 PTY 的两个 fd，避免反复重启时泄漏。"""
        self._pty_stop.set()
        for attr in ("_pty_master", "_pty_slave"):
            fd = getattr(self, attr, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, attr, None)

    def _note_console(self, text: str) -> None:
        with self._cond:
            self._program_console.append(text)

    def _gdb_set(self, name: str, value: str) -> bool:
        """设置一个 GDB 选项，返回是否成功。

        MI 的形式是 `-gdb-set 变量名 值`（**不要** `set` 前缀）。
        """
        try:
            result = self.send("-gdb-set %s %s" % (name, value), timeout=5.0)
        except GdbError:
            return False
        ok = bool(result) and result.get("kind") != "error"
        if not ok:
            # 记到 console 缓冲里，界面上能看到，不再无声无息
            self._console.append(
                "(cdbg) 设置 %s=%s 失败：%s\n"
                % (name, value, (result or {}).get("message", "无响应"))
            )
        if name == "new-console":
            self._new_console = ok
        return ok

    @staticmethod
    def _quote(text: str) -> str:
        """MI 里的路径要加引号，尤其是含空格时。"""
        return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')

    def close(self) -> None:
        """结束 GDB 进程。"""
        self._closed = True
        self._kill_proc()

    def _kill_proc(self) -> None:
        """干掉当前 GDB 进程（不动 _closed 标志，供内部重启用）。"""
        if self.proc is None:
            return
        # 先让这个进程的读线程「过期」：它退出时的 finally 会检查代号，
        # 认不出自己就什么都不改 —— 否则它会把重启后的新会话标成已关闭。
        self._proc_gen += 1
        # 被调试程序还活着的话先弄掉 —— 否则 GDB 会拒绝退出，
        # 或者留下一个孤儿进程。
        try:
            if not self._exited and self.proc.poll() is None:
                self.send_async("-interpreter-exec console \"kill\"")
                time.sleep(0.15)
        except Exception:
            pass
        try:
            if self.proc.poll() is None:
                try:
                    self._write("-gdb-exit\n")
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    try:
                        self.proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception:
            pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        self._close_pty()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------------
    # 低层 I/O
    # ------------------------------------------------------------------

    def _write(self, text: str) -> None:
        """写入一条命令。

        这里刻意用二进制写：Windows 上 Python 文本层的行缓冲对管道不生效，
        写出去的命令会卡在缓冲区里，GDB 根本收不到。
        """
        if self.proc is None or self.proc.stdin is None:
            raise GdbError("GDB 未启动")
        payload = text if text.endswith("\n") else text + "\n"
        self.proc.stdin.write(payload.encode("utf-8"))
        self.proc.stdin.flush()

    def _read_loop(self, proc, gen: int) -> None:
        """后台读 stdout。

        用 read1() 一次取走「当前可用的」原始字节，而不是等满缓冲区 ——
        否则 GDB 的响应会被缓冲住，整个会话看起来像卡死。

        proc/gen 由调用方传进来（而不是读 self.proc）：GDB 可能因为
        重试而被替换，旧线程不能去读新进程的管道，退出时也不能
        把新会话的状态改成「已关闭」。
        """
        try:
            assert proc is not None and proc.stdout is not None
            buf = b""
            while True:
                chunk = self._read_some(proc.stdout)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace").rstrip("\r")
                    self._dispatch(line)
        except Exception:
            pass
        finally:
            with self._cond:
                if gen == self._proc_gen:
                    self._closed = True
                    self._cond.notify_all()

    @staticmethod
    def _read_some(stream) -> bytes:
        """读「当前可用的」字节，不等缓冲区填满。"""
        if hasattr(stream, "read1"):
            return stream.read1(65536)
        # 兜底：裸文件对象用 os.read
        try:
            import os

            return os.read(stream.fileno(), 65536)
        except Exception:
            return b""

    def _drain_stderr(self, proc) -> None:
        """GDB 的 stderr 只是噪音，读掉避免管道写满导致 GDB 阻塞。"""
        try:
            assert proc is not None and proc.stderr is not None
            while True:
                chunk = self._read_some(proc.stderr)
                if not chunk:
                    break
        except Exception:
            pass

    def _dispatch(self, line: str) -> None:
        """把一行 MI 输出分派到对应位置。"""
        with self._cond:
            if line.startswith("^"):
                self._result = line
            elif line.startswith("*") or line.startswith("="):
                self._events.append(line)
                # 程序退出的通知：记个标志，免得别处再去问 GDB
                # （程序不在时 GDB 不会立刻回应查询，一问就是一次超时等待）
                if "thread-group-exited" in line:
                    self._exited = True
                    self._target_running = False
                elif line.startswith("*running"):
                    self._target_running = True
                    self._exited = False
                elif line.startswith("*stopped"):
                    self._target_running = False
            elif line.startswith("~"):
                self._console.append(line[1:])
            elif line.startswith("@"):
                pass
            elif line.startswith("(gdb)"):
                # 初始提示符：说明 GDB 已经进入 MI 命令循环，可以发命令了
                self._gdb_ready = True
            self._cond.notify_all()

    def _wait_ready(self, timeout: float = 10.0) -> bool:
        """等 GDB 输出初始提示符。

        这只是第一道门槛：提示符出现时，GDB 的 stdin 读取循环未必已经
        真正就绪 —— 此时写进去的命令会被静默丢弃（实测表现为 GDB 毫无
        反应，stdout 也不再有输出）。所以还要做一次主动探测。
        """
        deadline = time.time() + timeout
        while True:
            with self._cond:
                if self._gdb_ready:
                    return True
                closed = self._closed
            if closed and self.proc is not None and self.proc.poll() is not None:
                return False
            if time.time() >= deadline:
                return False
            time.sleep(0.02)

    def _probe_ready(self, attempts: int = 6, timeout: float = 1.5) -> bool:
        """主动探测 GDB 是否真的开始处理命令。

        发一条无害的同步命令（`-gdb-show` 打印设置），看能不能收到响应。
        收不到就重发 —— 因为前面被丢弃的命令不会产生任何副作用。
        """
        for _ in range(attempts):
            result = self.send("-gdb-show pagination", timeout=timeout, raise_on_timeout=False)
            if result is not None and result.get("kind") != "error":
                return True
            time.sleep(0.15)
        return False

    def _next_line(self, timeout: float) -> Optional[str]:
        """等下一个结果行（^ 开头）。

        这里刻意用「轮询 + 短暂睡眠」而不是 Condition.wait()：
        在 Windows 上，Condition 的唤醒路径在这个场景下不可靠 ——
        实测会出现「结果早已写入 _result，但 wait() 一直不返回」的情况，
        会话表现为完全卡死。轮询实现虽然土，但行为完全确定，
        20ms 的间隔在交互上完全无感。
        """
        deadline = time.time() + timeout
        while True:
            with self._cond:
                if self._result is not None:
                    result = self._result
                    self._result = None
                    return result
                closed = self._closed
            if closed and self.proc is not None and self.proc.poll() is not None:
                raise GdbError("GDB 进程已退出")
            if time.time() >= deadline:
                return None
            time.sleep(0.02)

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    def send(
        self, cmd: str, timeout: float = 30.0, raise_on_timeout: bool = True
    ) -> Optional[Dict[str, Any]]:
        """发送一条 MI 命令，返回解析后的结果。

        注意锁的粒度：只在「写入命令」这一瞬间持锁，等待响应期间必须放锁。
        因为读线程的 _dispatch 也需要这把锁来把结果写进 _result ——
        如果这里一直持锁不放，读线程永远拿不到锁（RLock 只在计数归零时
        才让别的线程进来），整个会话就会永久卡死。

        raise_on_timeout=False 时，超时返回 None 而不是抛异常。
        """
        with self._lock:
            self._write(cmd + "\n")
        line = self._next_line(timeout)

        if line is None:
            if raise_on_timeout:
                raise GdbError("GDB 命令超时: %s" % cmd)
            return None
        return self._parse_result(line)

    def send_async(self, cmd: str) -> None:
        """发送命令但不等待结果（用于 -exec-run 这类异步命令）。"""
        with self._lock:
            self._write(cmd + "\n")

    @staticmethod
    def _parse_result(line: str) -> Dict[str, Any]:
        """解析 ^done / ^error / ^running 等结果行。"""
        kind = line[1:].split(",", 1)[0].strip()
        out: Dict[str, Any] = {"kind": kind, "raw": line}
        if kind == "error":
            msg = ""
            idx = line.find('msg="')
            if idx >= 0:
                rest = line[idx + 5:]
                end = rest.find('"')
                msg = rest[:end] if end >= 0 else rest
            out["message"] = msg
            return out
        # 粗略解析 key="value" 对
        for token in _split_mi_tokens(line):
            key, _, value = token.partition("=")
            value = value.strip()
            if value.startswith("{") and value.endswith("}"):
                # 嵌套结构（如 bkpt={number="1",...}）
                out[key.strip()] = _parse_mi_value(value[1:-1])
            elif value.startswith('"') and value.endswith('"'):
                out[key.strip()] = value[1:-1]
            else:
                out[key.strip()] = value
        return out

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    def wait_event(self, prefix: str = "*stopped", timeout: float = 30.0) -> Optional[Dict[str, Any]]:
        """等待一个异步事件（如 *stopped）。"""
        deadline = time.time() + timeout
        with self._cond:
            while True:
                for idx, ev in enumerate(self._events):
                    if ev.startswith(prefix):
                        del self._events[idx]
                        return _parse_mi_dict(ev)
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                if self._closed and self.proc is not None and self.proc.poll() is not None:
                    # 进程退出前把剩余事件清空
                    for idx, ev in enumerate(self._events):
                        if ev.startswith(prefix):
                            del self._events[idx]
                            return _parse_mi_dict(ev)
                    return None
                self._cond.wait(min(remaining, 0.2))

    def drain_events(self) -> List[Dict[str, Any]]:
        """取出所有待处理事件。"""
        with self._cond:
            events = [_parse_mi_dict(ev) for ev in self._events]
            self._events.clear()
            return events

    def console_text(self) -> str:
        """取出累积的程序输出（PTY 里的 printf / scanf 回显）。"""
        with self._cond:
            text = "".join(self._program_console)
            self._program_console.clear()
            return text

    def console_write(self, data: str) -> bool:
        """把用户输入写进被调试程序的终端（PTY）。失败返回 False。"""
        if not data:
            return True
        with self._console_io_lock:
            master = self._pty_master
            if master is None:
                return False
            try:
                os.write(master, data.encode("utf-8", errors="replace"))
                return True
            except (OSError, ValueError):
                return False

    # ------------------------------------------------------------------
    # 执行控制
    # ------------------------------------------------------------------

    def set_breakpoint(self, location: str) -> Optional[int]:
        """在 `文件:行号` 或函数名处设置断点。返回断点号。

        一个坑：程序正在运行时，MI 对 -break-insert 的同步响应是 ^running
        而不是 ^done（GDB 会把命令排队，等程序停下来才真正执行）。
        这时候不能从响应里拿断点号，只能等一会儿再查断点表。
        """
        cmd = '-break-insert %s' % self._quote(location)
        result = self.send(cmd)
        num = self._extract_bkpt_number(result)
        # 顺手记下 GDB 实际把断点放在哪一行 —— 调用方要拿它纠正界面
        self.last_bkpt_line = self._extract_bkpt_line(result)

        if num is None:
            # 命令被排队了 —— 程序停下来之后它才会生效。
            # 断开前先清掉上一轮的值，免得把别的断点行号当成这次的结果。
            self.last_bkpt_line = None
            num = self._wait_breakpoint_at(location)

        if num:
            self._breakpoints[location] = num
            self._bp_count += 1
        return num

    @staticmethod
    def _extract_bkpt_line(result: Optional[Dict[str, Any]]) -> Optional[int]:
        """从 MI 的 ^done,bkpt={...} 里取出断点实际落到的行号。"""
        if not result:
            return None
        bkpt = result.get("bkpt")
        if isinstance(bkpt, dict):
            try:
                value = int(bkpt.get("line", 0))
            except (TypeError, ValueError):
                return None
            return value or None
        return None

    @staticmethod
    def _extract_bkpt_number(result: Optional[Dict[str, Any]]) -> Optional[int]:
        """从 MI 结果里取出断点号。"""
        if not result:
            return None
        bkpt = result.get("bkpt")
        if isinstance(bkpt, dict):
            try:
                return int(bkpt.get("number", 0))
            except (TypeError, ValueError):
                return None
        return None

    def _wait_breakpoint_at(self, location: str, timeout: float = 4.0) -> Optional[int]:
        """等一个断点在指定位置生效，返回它的编号。

        用于「程序正在运行时设断点」的场景 —— 那时 MI 的同步响应里
        没有断点号，只能等命令真正执行后再从断点表里查。
        顺手把 `line="N"`（GDB 实际落点）也取回来，否则调用方只能退回
        用户点的那一行，断点标记又会和真实停点错开。
        """
        import re

        deadline = time.time() + timeout
        target = location.replace("\\", "/")
        while time.time() < deadline:
            time.sleep(0.05)
            try:
                result = self.send("-break-list", timeout=2.0, raise_on_timeout=False)
            except GdbError:
                continue
            if result is None:
                continue
            raw = result.get("raw", "")
            # 断点表是一串 bkpt={...}，逐个比对原始位置再抠字段 ——
            # 直接全文找 number= 会拿到别的断点号。
            for chunk in re.findall(r"bkpt=\{(.*?)\}", raw):
                norm = chunk.replace("\\\\", "/").replace("\\", "/")
                if target not in norm:
                    continue
                num_m = re.search(r'number="(\d+)"', chunk)
                if not num_m:
                    continue
                line_m = re.search(r'line="(\d+)"', chunk)
                if line_m:
                    self.last_bkpt_line = int(line_m.group(1))
                return int(num_m.group(1))
        return None

    def set_line_breakpoint(self, line: int, source: str = "") -> Optional[int]:
        """在源码文件的某一行设置断点。返回 GDB 断点号。

        两个坑：
        1. 路径必须用正斜杠。GDB 内部把路径存成 POSIX 形式，
           传反斜杠的 Windows 路径会匹配不上符号表里的文件名。
        2. line 是**工作副本的**行号（符号表记的是编译时那份源码）。

        第三点：断点表按 **GDB 实际落到的行** 记账，不是按用户点的那行。
        用户点在空行 / 纯声明行上时，GDB 会往后吸附一两行；按点击行记账的话，
        界面上断点标记和程序实际停的位置会差一行，看起来就像「错层」。
        """
        path = (source or self.source_path or self.exe_path).replace("\\", "/")
        # 这一行已经有断点了就别再插一个：GDB 允许同一位置挂多个断点，
        # 插进去的第二个只会让「删一个还剩一个」变成固定的坑。
        existing = self._user_breakpoints.get(line)
        if existing:
            self.last_bkpt_line = line
            return existing

        num = self.set_breakpoint("%s:%d" % (path, line))
        if num:
            actual = self.last_bkpt_line or line
            # 吸附之后撞到已有的断点上（点第 22 行的空行，GDB 落到第 23 行，
            # 而第 23 行早就有断点了）——把新插的这个删掉，沿用旧断点号。
            old = self._user_breakpoints.get(actual)
            if old and old != num:
                try:
                    self.send("-break-delete %d" % num)
                except GdbError:
                    pass
                self._breakpoints.pop("%s:%d" % (path, line), None)
                return old
            self._user_breakpoints[actual] = num
        return num

    def clear_line_breakpoint(self, line: int) -> bool:
        """删除某个源码行上的断点。

        `_user_breakpoints` 是按 **GDB 实际落点** 记账的，而调用方可能拿
        「用户点的那一行」来清（吸附前）。只按落点查的话这种清法会失败，
        GDB 里那颗断点就留成了幽灵 —— 于是红点删掉后又自己长回来。
        所以再退一步，用 `_breakpoints`（按请求位置 `文件:行` 记的）兜底。
        """
        num = self._user_breakpoints.pop(line, None)
        if num is None:
            path = (self.source_path or self.exe_path or "").replace("\\", "/")
            num = self._breakpoints.pop("%s:%d" % (path, line), None)
            if num is None:
                return False
            # 同一个断点号在两张表里各有一份，另一份也要清掉
            for key, value in list(self._user_breakpoints.items()):
                if value == num:
                    self._user_breakpoints.pop(key, None)
                    break
        else:
            for key, value in list(self._breakpoints.items()):
                if value == num:
                    self._breakpoints.pop(key, None)
                    break
        try:
            self.send("-break-delete %d" % num)
            self._bp_count = max(0, self._bp_count - 1)
            return True
        except GdbError:
            return False

    def user_breakpoint_lines(self) -> List[int]:
        """当前用户设置的断点行号。"""
        return sorted(self._user_breakpoints.keys())

    def has_user_breakpoint(self) -> bool:
        """用户是否设置过断点。

        没有用户断点时，按 Dev-C++ 的行为，调试会停在程序入口
        （相当于 GDB 的 start 命令）。
        """
        return bool(self._user_breakpoints)

    def delete_breakpoints(self) -> None:
        """删除全部断点。"""
        try:
            self.send("-break-delete")
        except GdbError:
            pass
        self._breakpoints.clear()
        self._user_breakpoints.clear()
        self._bp_count = 0

    def run(self) -> None:
        """启动程序（异步）。"""
        self._exited = False
        self.send_async("-exec-run")

    def start_program(self) -> None:
        """启动程序并停在 main —— 等价于 GDB 的 start 命令。

        与 run 的区别：run 会一路跑下去，start 会在函数入口停下。
        Dev-C++ 在没有用户断点时用的就是 start。

        （方法名不能叫 start —— 那个名字属于「启动 GDB 进程」本身。）
        """
        self._exited = False
        self.send_async("-exec-run --start")

    def step_over(self) -> None:
        """单步跳过（不进入函数）。"""
        self.send_async("-exec-next")

    def step_into(self) -> None:
        """单步进入。"""
        self.send_async("-exec-step")

    def step_out(self) -> None:
        """运行到当前函数返回。"""
        self.send_async("-exec-finish")

    def continue_(self) -> None:
        """继续运行。"""
        self.send_async("-exec-continue")

    def run_to_line(self, line: int) -> Optional[int]:
        """运行到指定行（临时断点 + 继续）。

        对应 Dev-C++ 的「运行到光标」：tbreak 设临时断点，然后 continue。

        返回值是**临时断点实际落到的行**（工作副本坐标）—— 光标停在空行、
        纯声明行上时，GDB 会吸附到下一个有代码的行，界面得跟着挪过去，
        否则就会出现「光标在 18 行、程序停在 19 行」的错层。
        """
        path = (self.source_path or self.exe_path).replace("\\", "/")
        resolved = None
        try:
            self.last_bkpt_line = None
            self.send("-break-insert -t %s:%d" % (path, line))
            resolved = self.last_bkpt_line
        except GdbError:
            pass
        self.send_async("-exec-continue")
        return resolved

    def kill(self, timeout: float = 5.0) -> bool:
        """停住并杀掉被调试程序。返回「确认它已经死透」。

        顺序必须是**先 interrupt、再 kill**，不能直接发 kill：

        1. 程序跑着的时候，`-interpreter-exec console "kill"` 会回
           `^error,msg="Failed to resume program execution - ContinueDebugEvent
           failed (error 87)"` —— 因为在 Windows 上要杀掉运行中的进程，
           GDB 得先把调试事件收回（resume→continue 那一套），状态对不上。
        2. 而在这之前还得让 GDB **肯读 stdin**：没开 `mi-async` 时，
           程序一跑起来 GDB 就卡在 wait_for_inferior 里，后面发的命令
           连一个字都不会回（实测 6 秒无响应），观感就是「停止按钮失灵、
           接下来点调试也起不来」。

        所以：`-exec-interrupt`（回 ^done + `*stopped signal-received`）
        → `console kill`（回 `=thread-group-exited`）。全程几十毫秒。

        返回值：True = 确认程序已退出；False = 没等到退出事件。
        """
        if self.proc is None or self.proc.poll() is not None:
            return True

        # 1) 先让它停住。没在跑的时候这条会回 ^error（"not being run"），忽略即可。
        if self._target_running:
            self.send("-exec-interrupt", timeout=1.5, raise_on_timeout=False)
            # 把这个 *stopped 消费掉：它是「被中断」而不是「执行到某处」，
            # 留给后面的采集逻辑会拿到 ntdll 里的假栈帧。
            self.wait_event("*stopped", timeout=1.5)

        # 2) 再杀。GDB 会回 =thread-group-exited，进程的控制台窗口随之消失。
        self.send(
            '-interpreter-exec console "kill"', timeout=2.0, raise_on_timeout=False
        )

        dead = self.wait_program_exit(timeout=max(0.5, timeout - 3.0))
        if dead:
            self._target_running = False
        return dead

    def wait_program_exit(self, timeout: float = 3.0) -> bool:
        """等被调试程序彻底结束。返回是否成功。

        不能靠问 GDB「程序还在吗」—— 程序正跑着的时候 GDB 不回应查询
        （命令会排队），每问一次就是一次超时等待，卡十几秒。

        靠事件判断就快得多：kill 之后 GDB 会发 =thread-group-exited。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.03)
            with self._cond:
                if self._exited:
                    return True
                for ev in self._events:
                    if "thread-group-exited" in ev:
                        self._exited = True
                        return True
        # 事件没等到，退一步问 GDB（这次给足超时）
        try:
            r = self.send("-thread-info", timeout=1.2, raise_on_timeout=False)
            if r is None:
                self._exited = True
                return True
            alive = 'id="i1"' in r.get("raw", "")
            self._exited = not alive
            return not alive
        except GdbError:
            self._exited = True
            return True

    @property
    def target_running(self) -> bool:
        """被调试程序是不是正在跑（靠 *running / *stopped 事件维护）。"""
        return self._target_running

    def is_alive(self) -> bool:
        """被调试程序是否还在运行。"""
        try:
            r = self.send("-thread-info", timeout=3, raise_on_timeout=False)
            if r is None:
                return False
            # running 表示正在跑；stopped 表示停在某处（还活着）
            return r.get("kind") != "error"
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 快照同步
    # ------------------------------------------------------------------

    def read_snapshot(self, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
        """读探针写出的快照；等待 seq 递增（说明是新的一次采集）。"""
        import io
        import json

        deadline = time.time() + timeout
        while True:
            try:
                with io.open(self.snapshot_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                data = None

            if data is not None and data.get("seq", 0) != self._last_seq:
                self._last_seq = data.get("seq", 0)
                return data

            if time.time() >= deadline:
                return data
            time.sleep(0.03)

    def wait_new_snapshot(self, since_seq: int, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        """等一份**比 given_seq 更新**的快照。

        普通的 read_snapshot 在超时时会把旧数据也返回 —— 这在「重启」
        场景下会出问题：kill 掉旧程序时探针也会写一份快照，
        那一份是旧的（没有栈帧），拿它当结果就等于什么都没发生。
        """
        import io
        import json

        deadline = time.time() + timeout
        while True:
            try:
                with io.open(self.snapshot_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                data = None

            if data is not None and data.get("seq", 0) > since_seq:
                self._last_seq = data.get("seq", 0)
                return data

            if time.time() >= deadline:
                return None
            time.sleep(0.03)

    @property
    def last_seq(self) -> int:
        """当前已消费的快照序号。"""
        return self._last_seq

    def _read_seq(self) -> int:
        """读快照文件里的 seq（不改变 _last_seq）。读不到当 0。"""
        import io
        import json

        try:
            with io.open(self.snapshot_path, "r", encoding="utf-8") as fh:
                return int(json.load(fh).get("seq", 0))
        except Exception:
            return 0

    def sync_seq(self) -> int:
        """把 _last_seq 同步到文件里的最新序号。

        用于「我已经消费过这份快照」的场景（比如未执行时采集过一次），
        避免后面 wait_new_snapshot 把旧快照当新的。
        """
        self._last_seq = self._read_seq()
        return self._last_seq

    def sync_settled(self, quiet: float = 0.12, timeout: float = 1.5) -> int:
        """等快照文件「不再变化」后再同步 _last_seq。

        为什么需要它：杀掉程序的那一下，探针手里可能还压着一次采集 ——
        「中断」那个 *stopped 让它开始写盘，等它写完可能已经是我们
        下一次 `-exec-run` 之后了。直接 sync_seq() 会读到旧序号，
        于是下一条命令的 read_snapshot 立刻拿到那份「已退出」的快照，
        界面就会显示「程序已退出」，而程序其实好好停在 main 上。

        做法很简单：一直盯 seq，连续 quiet 秒没变才算稳定。
        """
        deadline = time.time() + timeout
        last = -1
        stable_at = None
        while time.time() < deadline:
            seq = self._read_seq()
            if seq != last:
                last = seq
                stable_at = time.time()
            elif stable_at is not None and time.time() - stable_at >= quiet:
                break
            time.sleep(0.02)
        self._last_seq = max(0, last)
        return self._last_seq


# ---------------------------------------------------------------------------
# MI 行解析工具
# ---------------------------------------------------------------------------


def _split_mi_tokens(text: str) -> List[str]:
    """把 MI 的 key="value" 列表切成 token 数组。

    要点：**按花括号深度切分**，不能在顶层逗号处一刀切。
    MI 的一行长这样：

        ^done,bkpt={number="1",type="breakpoint",file="a.c",line="26"},
             thread-groups=["i1"]

    顶层逗号切分会把 bkpt 内部和外部混在一起，得到
    `bkpt={number="1"` 这种截断的碎片 —— 断点号就丢了。
    """
    tokens: List[str] = []
    buf: List[str] = []
    in_quotes = False
    escaped = False
    depth = 0

    for ch in text:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            buf.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
            continue
        if not in_quotes:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == "," and depth == 0:
                tokens.append("".join(buf).strip())
                buf = []
                continue
        buf.append(ch)
    if buf:
        tokens.append("".join(buf).strip())
    return [t for t in tokens if t]


def _parse_mi_dict(text: str) -> Dict[str, Any]:
    """把 `*stopped,reason="x",frame={...}` 解析成嵌套字典。"""
    head, _, tail = text.partition(",")
    out: Dict[str, Any] = {"_kind": head.strip()}
    if not tail:
        return out

    for token in _split_mi_tokens(tail):
        key, _, value = token.partition("=")
        key = key.strip()
        value = value.strip()
        if value.startswith("{") and value.endswith("}"):
            out[key] = _parse_mi_value(value[1:-1])
        elif value.startswith('"') and value.endswith('"'):
            out[key] = value[1:-1]
        else:
            out[key] = value
    return out


def _parse_mi_value(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for token in _split_mi_tokens(text):
        key, _, value = token.partition("=")
        key = key.strip()
        value = value.strip()
        if value.startswith("{") and value.endswith("}"):
            out[key] = _parse_mi_value(value[1:-1])
        elif value.startswith('"') and value.endswith('"'):
            out[key] = value[1:-1]
        else:
            out[key] = value
    return out
