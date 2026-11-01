"""ecgb.webmon —— 网页实时监视（stdlib 实现，板上离线可用）。

Hub: 线程安全数据中枢（最新帧 / 事件 / 客户端计数 / 命令转发）；
WebMonitorServer: ThreadingHTTPServer + SSE(/events) + JSON API + 静态文件（static/ 三件套）。
"""
from .hub import Hub  # noqa: F401
from .server import WebMonitorServer  # noqa: F401

__all__ = ["Hub", "WebMonitorServer"]
