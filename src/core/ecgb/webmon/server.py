"""server.py —— 网页监视 HTTP 服务（stdlib：ThreadingHTTPServer + SSE）。

路由：/ 与 /static/<file>（带路径穿越防护）· /events（SSE 帧 + 事件 + keepalive）· /api/status ·
/api/events?since= · POST /api/command · /healthz。每连接一线程；序列化在连接线程做。
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .hub import Hub

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "application/javascript; charset=utf-8",
         ".json": "application/json; charset=utf-8",
         ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


def _jsonable(obj: Any) -> Any:
    """把 payload 里的 numpy/元组递归转成可 json.dumps 的原生类型。

    float 一律做有限性检查：NaN/Infinity 会产出非法 JSON（浏览器 JSON.parse 抛错 →
    画面静默停住），统一降级为 None。
    """
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    tolist = getattr(obj, "tolist", None)
    if callable(tolist):
        return _jsonable(tolist())
    item = getattr(obj, "item", None)
    if callable(item):
        try:
            return _jsonable(obj.item())
        except Exception:
            pass
    return str(obj)


class _Handler(BaseHTTPRequestHandler):
    server_version = "ecgb-webmon/1.0"
    protocol_version = "HTTP/1.1"
    hub: Hub = None                     # 由 WebMonitorServer 注入
    verbose = True
    token: str = ""                     # 非空则 /api/* 与命令需要 ?token= 或 X-Auth-Token

    def _check_token(self) -> bool:
        if not self.token:
            return True
        qs = parse_qs(urlparse(self.path).query)
        given = (qs.get("token") or [""])[0] or (self.headers.get("X-Auth-Token") or "")
        return given == self.token

    # ------------------------------------------------------------ 日志
    def log_message(self, fmt: str, *args: Any) -> None:      # noqa: A003
        if self.verbose:
            print(f"[webmon] {self.address_string()} {fmt % args}", flush=True)

    def log_error(self, fmt: str, *args: Any) -> None:        # noqa: A003
        print(f"[webmon] {self.address_string()} {fmt % args}", flush=True)

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:                                  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send_file(os.path.join(_STATIC_DIR, "index.html"))
        if path.startswith("/static/"):
            rel = path[len("/static/"):].replace("\\", "/")
            base = os.path.abspath(_STATIC_DIR)
            target = os.path.abspath(os.path.normpath(os.path.join(base, rel)))
            # 目录边界校验：commonpath 才能挡住 static_evil/ 这类前缀相同的兄弟目录
            try:
                inside = os.path.commonpath([target, base]) == base
            except ValueError:                        # 跨盘符等异常
                inside = False
            if not inside or ".." in rel.split("/"):
                return self._send_json({"error": "forbidden"}, code=403)
            return self._send_file(target)
        if path == "/events":
            return self._sse()
        if path == "/api/status":
            if not self._check_token():
                return self._send_json({"error": "unauthorized"}, code=401)
            return self._send_json({"server": self.hub.status(),
                                    "host": self.hub.host_status(),
                                    "meta": self.hub.meta})
        if path == "/api/events":
            qs = parse_qs(urlparse(self.path).query)
            try:
                since = int((qs.get("since") or ["-1"])[0])
            except (TypeError, ValueError):
                return self._send_json({"error": "bad since"}, code=400)
            return self._send_json({"events": _jsonable(self.hub.events_since(since))})
        if path == "/healthz":
            return self._send_json({"ok": True})
        if path == "/favicon.ico":
            self.send_response(204)                    # 204 不带 Content-Length（RFC 7230）
            self.end_headers()
            return
        return self._send_json({"error": "not found", "path": path}, code=404)

    # ------------------------------------------------------------ POST
    def do_POST(self) -> None:                                 # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/command":
            return self._send_json({"error": "not found"}, code=404)
        if not self._check_token():
            return self._send_json({"ok": False, "error": "unauthorized"}, code=401)
        body = self._read_body()
        if body is None:
            self.close_connection = True             # 体不完整：直连不再复用，避免串包
            return self._send_json({"ok": False, "error": "bad or oversized body"}, code=400)
        try:
            data = json.loads(body or b"{}")
        except Exception as exc:
            return self._send_json({"ok": False, "error": f"bad json: {exc}"}, code=400)
        target = str(data.get("target") or "")
        action = str(data.get("action") or "")
        arg = data.get("arg")
        if not target or not action:
            return self._send_json({"ok": False, "error": "target/action 必填"}, code=400)
        ok = self.hub.submit(target, action, arg)
        return self._send_json({"ok": ok, "queued": ok, "target": target, "action": action})

    # ------------------------------------------------------------ 工具
    _MAX_BODY = 64 * 1024                            # 命令体上限（防超大 Content-Length 拖死）

    def _read_body(self) -> Optional[bytes]:
        """读请求体；非法/超限返回 None（调用方回 400/413 并关连接）。"""
        if (self.headers.get("Transfer-Encoding") or "").lower().strip() == "chunked":
            return None                              # 本服务不支持 chunked（保持连接不串包）
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            return None
        if n < 0 or n > self._MAX_BODY:
            return None
        return self.rfile.read(n) if n > 0 else b""

    def _send_json(self, obj: Any, code: int = 200) -> None:
        try:
            payload = json.dumps(_jsonable(obj), ensure_ascii=False,
                                 allow_nan=False).encode("utf-8")
        except (ValueError, TypeError):
            payload = b'{"error":"serialize failed"}'
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_file(self, path: str) -> None:
        if not os.path.isfile(path):
            return self._send_json({"error": "not found", "file": os.path.basename(path)},
                                   code=404)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            return self._send_json({"error": f"read fail: {exc}"}, code=500)
        ext = os.path.splitext(path)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", _MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ------------------------------------------------------------ SSE
    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        hub = self.hub
        stop_ev = getattr(self.server, "stop_event", None)
        try:
            self.connection.settimeout(15.0)           # 客户端卡死时写操作不会永久阻塞
        except Exception:
            pass
        hub.add_client()
        # 事件从"当前序号"起算：不回灌历史上全部事件（审计 P2）
        try:
            last_ev = int(hub.status().get("seq", 0)) - 1
        except Exception:
            last_ev = -1
        # 注意：last_seq 必须是"已发送帧的序号"（初值 -1 → 立刻补发最新帧；
        # 之后 wait_frame 才会真正阻塞等新帧）。写成常量会让同一帧无限重发。
        last_seq = -1
        # snapshot 帧按 snapshot_interval 节流：间隔内的帧只推进序号、不写网络，到点推最新一帧
        # （帧合并，客户端无感）。alarm 事件低频且是演示关键，**不受节流**、即时推送；
        # 置 0 可恢复"来一帧推一帧"的行为（排查用）。
        try:
            interval = float(getattr(self.server, "snapshot_interval", 0.1) or 0.0)
        except (TypeError, ValueError):
            interval = 0.1
        if interval < 0.0:
            interval = 0.0
        last_push = 0.0
        try:
            self._sse_write("hello", {"meta": hub.meta, "server": hub.status()})
            while True:
                if stop_ev is not None and stop_ev.is_set():
                    break
                got = hub.wait_frame(last_seq, timeout=5.0)
                if got is not None:
                    last_seq, frame = got
                    now = time.monotonic()
                    if interval <= 0.0 or (now - last_push) >= interval:
                        self._sse_write("snapshot", frame)
                        last_push = now
                events = hub.events_since(last_ev)
                for ev in events:
                    last_ev = max(last_ev, int(ev.get("seq", last_ev)))
                    self._sse_write("alarm", ev)
                if got is None and not events:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            hub.remove_client()

    def _sse_write(self, event: str, payload: Dict[str, Any]) -> None:
        body = json.dumps(_jsonable(payload), ensure_ascii=False, separators=(",", ":"))
        chunk = f"event: {event}\ndata: {body}\n\n".encode("utf-8")
        self.wfile.write(chunk)
        self.wfile.flush()


class WebMonitorServer:
    """把 Hub 暴露成 HTTP 服务（后台 daemon 线程，start/stop 幂等）。"""

    def __init__(self, hub: Hub, host: str = "0.0.0.0", port: int = 8765,
                 verbose: bool = True, token: str = "",
                 snapshot_interval: float = 0.1) -> None:
        self.hub = hub
        self.host = host
        self.port = int(port)
        self.verbose = verbose
        self.token = str(token or "")
        # SSE snapshot 推送最小间隔（秒）；0 = 不节流
        try:
            self.snapshot_interval = max(0.0, float(snapshot_interval))
        except (TypeError, ValueError):
            self.snapshot_interval = 0.1
        self.stop_event = threading.Event()                # SSE 线程据此退出（审计 P1）
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._th: Optional[threading.Thread] = None

    def start(self) -> str:
        if self._httpd is not None:
            return self.url()
        handler = type("BoundHandler", (_Handler,), {"hub": self.hub,
                                                     "verbose": self.verbose,
                                                     "token": self.token})
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = int(self._httpd.server_address[1])     # port=0 时取系统实分配端口
        self._httpd.daemon_threads = True
        self._httpd.stop_event = self.stop_event           # 供 _Handler 读取
        self._httpd.snapshot_interval = self.snapshot_interval   # 供 _sse 节流读取
        self._th = threading.Thread(target=self._httpd.serve_forever,
                                    kwargs={"poll_interval": 0.2},
                                    name="webmon-http", daemon=True)
        self._th.start()
        return self.url()

    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}/"

    def stop(self) -> None:
        if self._httpd is None:
            return
        self.stop_event.set()                              # 先让 SSE 线程退出循环
        try:
            self.hub.wake()                                # 唤醒阻塞在 wait_frame 的 SSE 线程
        except Exception:
            pass
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass
        self._httpd = None
        self._th = None
