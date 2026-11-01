"""hub.py —— 网页监视的数据中枢（线程安全）。

采集线程 publish(frame) → Hub → SSE 线程 wait_frame() → 浏览器；命令走 Hub.submit() →
PluginHost 队列 → 采集线程执行。
约束：publish 必须 O(1)；frame 可放 numpy 数组（序列化在 SSE 线程）；事件 seq 单调递增。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


class Hub:
    def __init__(self, *, submit: Optional[Callable[[str, str, Any], bool]] = None,
                 status_provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 events_provider: Optional[Callable[[int], List[Dict[str, Any]]]] = None,
                 meta: Optional[Dict[str, Any]] = None) -> None:
        self._cond = threading.Condition()
        self._frame: Optional[Dict[str, Any]] = None
        self._seq = 0
        self._t0 = time.time()
        self._clients = 0
        self._n_frames = 0
        self._n_commands = 0
        self._submit = submit
        self._status_provider = status_provider
        self._events_provider = events_provider
        self.meta: Dict[str, Any] = dict(meta or {})

    # ------------------------------------------------------------- 采集侧
    def publish(self, frame: Dict[str, Any]) -> None:
        """采集线程调用：替换最新帧并唤醒等待的 SSE 线程。"""
        with self._cond:
            self._frame = frame
            self._seq += 1
            self._n_frames += 1
            self._cond.notify_all()

    # ------------------------------------------------------------- SSE 侧
    def wait_frame(self, last_seq: int, timeout: float = 10.0) -> Optional[Tuple[int, Dict[str, Any]]]:
        """等新帧；超时返回 None（调用方发 keepalive）。"""
        with self._cond:
            if last_seq >= self._seq or self._frame is None:
                self._cond.wait(timeout)
            if self._seq > last_seq and self._frame is not None:
                return self._seq, self._frame
            return None

    def wake(self) -> None:
        """唤醒所有在 wait_frame 里等待的线程（服务停止时让 SSE 立刻醒来退出）。"""
        with self._cond:
            self._cond.notify_all()

    def add_client(self) -> None:
        with self._cond:
            self._clients += 1

    def remove_client(self) -> None:
        with self._cond:
            self._clients = max(0, self._clients - 1)

    # ------------------------------------------------------------- API 侧
    def submit(self, target: str, action: str, arg: Any = None) -> bool:
        if self._submit is None:
            return False
        with self._cond:                              # 与 status() 的读取保持同一把锁
            self._n_commands += 1
        return bool(self._submit(target, action, arg))

    def events_since(self, seq: int) -> List[Dict[str, Any]]:
        if self._events_provider is None:
            return []
        try:
            return list(self._events_provider(seq))
        except Exception:
            return []

    def host_status(self) -> Dict[str, Any]:
        if self._status_provider is None:
            return {}
        try:
            return dict(self._status_provider())
        except Exception:
            return {}

    def status(self) -> Dict[str, Any]:
        with self._cond:
            return {
                "uptime_s": round(time.time() - self._t0, 1),
                "seq": self._seq,
                "n_frames": self._n_frames,
                "clients": self._clients,
                "n_commands": self._n_commands,
                "has_frame": self._frame is not None,
            }
