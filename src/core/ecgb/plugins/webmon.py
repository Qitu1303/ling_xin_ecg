"""webmon.py —— 网页实时监视桥（P0 插件，零接口改动）。

把每帧快照 + 各分析插件最新结果组装成 payload 交给 webmon.Hub，由 HTTP/SSE 推给浏览器；
组装只做浅拷贝与引用，序列化在 SSE 线程完成。参数见 --plugins-file（port / max_points 等）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..webmon import Hub, WebMonitorServer
from .base import PluginBase, register

_VERSION = "2026-10-15"


@register
class WebMonitor(PluginBase):
    name = "webmon"
    priority = 98                 # 最后组装：等分析插件都写完了 latest
    description = "网页实时监视（HTTP + SSE，板上离线可用）"

    def __init__(self, port: int = 8765, host: str = "0.0.0.0",
                 max_points: int = 1500, push_wave: bool = True,
                 token: str = "", url_hint: str = "",
                 snapshot_interval: float = 0.1) -> None:
        super().__init__(port=port, host=host, max_points=max_points,
                         push_wave=push_wave, token=token, url_hint=url_hint,
                         snapshot_interval=snapshot_interval)
        self.hub: Optional[Hub] = None
        self.server: Optional[WebMonitorServer] = None
        self._url = ""

    # ---------------------------------------------------------------- 生命周期
    def on_start(self) -> None:
        host_obj = self.ctx.host
        self.hub = Hub(
            submit=(host_obj.submit if host_obj else None),
            status_provider=(host_obj.status if host_obj else None),
            events_provider=(host_obj.events if host_obj else None),
            meta={
                "version": _VERSION,
                "fs": self.ctx.fs,
                "note": "教学演示用；非医疗诊断",
                "plugins": [p.name for p in (host_obj.plugins if host_obj else [])],
            },
        )
        self.server = WebMonitorServer(self.hub, host=self.params["host"],
                                       port=int(self.params["port"]),
                                       verbose=self.ctx.verbose,
                                       token=str(self.params.get("token") or ""),
                                       snapshot_interval=self.params.get("snapshot_interval", 0.1))
        try:
            self._url = self.server.start()
        except OSError as exc:
            self.ctx.log(f"端口 {self.params['port']} 起服务失败：{exc!r} → 本插件停用")
            self.server = None
            self.enabled = False
            return
        self.ctx.log(f"网页监视已启动：{self._url}（本机浏览器打开；板端用板子 IP:"
                     f"{self.params['port']}）")

    def on_snapshot(self, snap) -> None:
        if self.hub is None:
            return
        if self.hub.status()["clients"] == 0:       # 没观众就不组装（省 CPU，审计 P2）
            return
        wave = snap.wave
        payload_wave = None
        n_wave = int(len(wave)) if wave is not None else 0
        wave_start = int(snap.n_total) - n_wave
        step = max(1, n_wave // max(1, int(self.params["max_points"])))
        if self.params["push_wave"] and n_wave:
            payload_wave = wave[::step]
        extra = snap.extra or {}
        beats = extra.get("beats") or {}
        self.hub.publish({
            "t": time.time(),
            "mono": float(snap.t or 0.0),
            "fs": float(snap.fs or 0.0),
            "hr": snap.hr,
            "hr_ok": bool(snap.hr_ok),
            "dropped": int(snap.dropped or 0),
            "n_total": int(snap.n_total or 0),
            "n_hw": int(snap.n_hw or 0),
            "wave": payload_wave,
            "wave_step": step,
            "wave_start": wave_start,
            "beats": {"idx_abs": beats.get("idx_abs", ()),
                      "n_beats": beats.get("n_beats")},
            "health": {
                "est_fs_sw": extra.get("est_fs_sw"),
                "dup_ratio": extra.get("dup_ratio"),
                "missed_poll_ratio": extra.get("missed_poll_ratio"),
                "last_error": extra.get("last_error", ""),
            },
            "analysis": {
                "quality": self.ctx.latest("quality"),
                "alarm": self.ctx.latest("alarm"),
                "hrv": self.ctx.latest("hrv"),
                "arrhythmia": self.ctx.latest("arrhythmia"),
                "intervals": self.ctx.latest("intervals"),
            },
            "session": {
                "recorder": self.ctx.latest("recorder"),
                "quick30": self.ctx.latest("quick30"),
            },
        })

    def on_command(self, action: str, arg: Any = None) -> Any:
        if action == "url":
            return self._url
        return None

    def on_close(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.ctx.log("网页监视已停止")

    def stats(self) -> Dict[str, Any]:
        st = super().stats()
        st.update({"url": self._url,
                   "clients": (self.hub.status()["clients"] if self.hub else 0)})
        return st
