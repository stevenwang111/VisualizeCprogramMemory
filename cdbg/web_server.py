"""Web 后端：静态托管 + REST + SSE 推送。

刻意不引入任何第三方依赖 —— 标准库的 ThreadingHTTPServer 足够，
这样用户不用装 Flask/FastAPI，一条命令就能跑起来。
"""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .oplogger import OpLogger
from .session import DebugSession
from .snapshot import Snapshot

WEB_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")


class _Server(ThreadingHTTPServer):
    """不该把浏览器的「正常断线」当成服务器错误打出来。

    浏览器预连接、刷新页面、关标签页，都会让一条已经建立的 TCP 连接
    被本机协议栈直接中止 —— 服务端读到一半就收到 WinError 10053
    （ConnectionAbortedError）或 10054（ConnectionResetError）。
    socketserver 默认的处理是把整段 traceback 打到终端上，于是每刷新
    一次页面，控制台就糊一片红字，把真正的调试输出淹掉。

    这里把「对端断线」这一类和 handler 自身的 bug 区分开：前者静默，
    后者照旧打印 traceback。
    """

    def handle_error(self, request, client_address):
        import sys
        import traceback

        exc = sys.exc_info()[1]
        if isinstance(
            exc,
            (
                ConnectionAbortedError,  # WinError 10053
                ConnectionResetError,  # WinError 10054
                BrokenPipeError,
                ConnectionError,
                socket.timeout,
                TimeoutError,
            ),
        ):
            return
        traceback.print_exc()


class _Handler(BaseHTTPRequestHandler):
    server_version = "cdbg/0.1"
    session: Optional[DebugSession] = None

    # 静默默认的日志输出
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _log_command(self, cmd: str, args: Dict[str, Any], result: Dict[str, Any],
                     seconds: float) -> None:
        """把一个命令的结果记进操作日志。

        命令行这一行是复盘的关键：前端只知道「我发了 next」，
        真正耗了多久、有没有等到程序停下、停到哪一行，只有这里知道。
        """
        logger = getattr(self, "oplog", None)
        if logger is None:
            return
        try:
            fields = {
                "cmd": cmd,
                "ok": 1 if result.get("ok") else 0,
                "ms": int(seconds * 1000),
            }
            if args.get("line"):
                fields["arg_line"] = args.get("line")
            if result.get("waiting"):
                fields["waiting"] = 1
            if "killed" in result:
                fields["killed"] = 1 if result.get("killed") else 0
            if result.get("line"):
                fields["line"] = result.get("line")
            if result.get("message"):
                fields["msg"] = result.get("message")
            logger.write("CMD", cmd, fields)
        except Exception:
            pass

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            return self._serve_static("index.html")
        if route == "/favicon.ico":
            # 浏览器总会要一个 favicon，回一个空的避免 404 噪音
            self.send_response(204)
            self.end_headers()
            return
        if route == "/api/state":
            if self.session is None:
                return self._send_json({"ok": False, "message": "没有正在进行的调试会话"})
            return self._send_json(self.session.state())
        if route == "/api/events":
            return self._serve_sse()
        if route == "/api/oplog":
            logger = getattr(self, "oplog", None)
            if logger is None:
                return self._send_json({"ok": False, "message": "操作日志未启用"}, 404)
            info = logger.info()
            info["lines"] = logger.read_lines()
            return self._send_json(info)
        if route.startswith("/api/"):
            return self._send_json({"ok": False, "message": "未知接口"}, 404)
        return self._serve_static(route.lstrip("/"))

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        # 操作日志要先于 session 检查：即便会话没起来，也要能把前端事件记下来。
        # 这里永远回 ok —— 记录失败绝不能让前端的命令流程报错。
        if route == "/api/oplog":
            logger = getattr(self, "oplog", None)
            if logger is not None and isinstance(payload, dict):
                try:
                    logger.write(
                        str(payload.get("type", "event") or "event"),
                        str(payload.get("message", "") or ""),
                        {k: v for k, v in payload.items() if k not in ("type", "message")},
                    )
                except Exception:
                    pass
            return self._send_json({"ok": True})

        if self.session is None:
            return self._send_json({"ok": False, "message": "没有正在进行的调试会话"}, 400)

        # payload 里本来就带 cmd，不能再当关键字传一遍
        args = {k: v for k, v in payload.items() if k != "cmd"}
        cmd = payload.get("cmd", "")

        try:
            if route == "/api/command":
                started = time.time()
                result = self.session.command(cmd, **args)
                self._log_command(cmd, args, result, time.time() - started)
                return self._send_json(result)
            if route == "/api/break":
                result = self.session.command("break", **args)
                return self._send_json(result)
        except Exception as exc:
            import traceback

            traceback.print_exc()
            return self._send_json(
                {"ok": False, "message": "命令执行失败: %s" % str(exc)[:200]}, 500
            )

        return self._send_json({"ok": False, "message": "未知接口"}, 404)

    # ------------------------------------------------------------------
    # 静态文件
    # ------------------------------------------------------------------

    def _serve_static(self, rel: str) -> None:
        rel = posixpath.normpath(rel).lstrip("/")
        if not rel or rel == ".":
            rel = "index.html"
        full = os.path.normpath(os.path.join(WEB_ROOT, rel))
        # 防目录穿越
        if not os.path.abspath(full).startswith(os.path.abspath(WEB_ROOT)):
            return self._send_json({"ok": False}, 403)
        if not os.path.isfile(full):
            return self._send_json({"ok": False, "message": "文件不存在: %s" % rel}, 404)
        ctype, _ = mimetypes.guess_type(full)
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            self._send_bytes(fh.read(), ctype)

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------

    def _serve_sse(self) -> None:
        """Server-Sent Events：程序状态一变就推一份新快照。"""
        session = self.session
        if session is None:
            self._send_json({"ok": False}, 400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        queue: list = []
        lock = threading.Lock()
        ready = threading.Event()

        def listener(snap: Snapshot):
            with lock:
                queue.append(snap)
            ready.set()

        session.subscribe(listener)
        try:
            # 先推当前状态
            if session.snapshot is not None:
                self._push(session.snapshot)
            else:
                self._push(None)

            while True:
                ready.wait(timeout=1.0)
                ready.clear()
                with lock:
                    pending = list(queue)
                    queue.clear()
                for snap in pending:
                    self._push(snap)
                if not pending:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            session.unsubscribe(listener)

    def _push(self, snap: Optional[Snapshot]) -> None:
        payload = snap.to_json() if snap is not None else None
        if payload is not None:
            # 把会话阶段一起推上去，前端据此决定哪些按钮可点
            payload["phase"] = self.session.phase if self.session else "not-started"
            if self.session is not None:
                try:
                    payload["breakpoints"] = self.session.breakpoint_lines()
                except Exception:
                    payload["breakpoints"] = []
        data = json.dumps(payload, ensure_ascii=False)
        self.wfile.write(b"data: " + data.encode("utf-8") + b"\n\n")
        self.wfile.flush()


def serve(session: DebugSession, host: str = "127.0.0.1", port: int = 0):
    """启动 Web 服务，返回 (server, 实际端口)。"""
    # 记录器独立于会话创建：日志文件名就是这次启动的时刻。
    try:
        oplog = OpLogger()
    except Exception:
        oplog = None
    handler = type("_BoundHandler", (_Handler,), {"session": session, "oplog": oplog})
    httpd = _Server((host, port), handler)
    httpd.daemon_threads = True
    # 把记录器挂到会话上：命令执行期间（停止 / 重开 / 采集）的内部动作
    # 只有会话自己知道，光靠前端点击日志是看不出来的。
    try:
        session.oplog = oplog
    except Exception:
        pass
    if oplog is not None:
        oplog.write("SESSION", "调试会话启动", {"program": getattr(session, "original_source_path", "")})
    return httpd, httpd.server_address[1]
