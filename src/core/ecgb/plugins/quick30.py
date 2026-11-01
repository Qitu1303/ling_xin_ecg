"""quick30.py —— 30 秒快速测量模式（P0，零接口改动）。

一键体检：汇总 30 s 窗口的心率统计、质量概况、节律事件与 HRV 30 s 窗（未启用则标注），
收尾写 quick30_YYYYmmdd_HHMMSS.json 并上抛 info 事件。命令：start / stop / status。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

import numpy as np

from .base import PluginBase, register


@register
class Quick30(PluginBase):
    name = "quick30"
    priority = 95
    description = "30 秒快速测量：一键汇总心率/质量/节律/HRV 并出报告"

    def __init__(self, duration: float = 30.0, auto_start: bool = False,
                 min_valid_ratio: float = 0.5) -> None:
        super().__init__(duration=duration, auto_start=auto_start,
                         min_valid_ratio=min_valid_ratio)
        self.active = False
        self._t0: Optional[float] = None
        self._hrs: List[float] = []
        self._n_total = 0
        self._n_ok = 0
        self._n_no_hr = 0
        self._events_at_start = 0
        self._last_report: Optional[Dict[str, Any]] = None

    # ---------------------------------------------------------------- 生命周期
    def on_start(self) -> None:
        if self.params["auto_start"]:
            self.start()

    def start(self) -> bool:
        if self.active:
            return True
        self.active = True
        self._t0 = None
        self._hrs = []
        self._n_total = 0
        self._n_ok = 0
        self._n_no_hr = 0
        self._events_at_start = len(self.ctx.host.events()) if self.ctx.host else 0
        self.ctx.log(f"开始 {self.params['duration']:.0f} s 快速测量……")
        self.ctx.emit_event("info", f"开始 {self.params['duration']:.0f} 秒快速测量",
                            "quick30_start")
        self._publish(progress=0.0)
        return True

    def stop(self, reason: str = "user") -> None:
        if self.active:
            self.active = False
            self.ctx.emit_event("low", f"快速测量被中止（{reason}）", "quick30_abort")
            self._publish(progress=0.0)

    # ---------------------------------------------------------------- 数据
    def on_snapshot(self, snap) -> None:
        if not self.active:
            return
        if self._t0 is None:
            self._t0 = float(snap.t)
        self._n_total += 1                        # 分母=激活期间的全部快照（含无 HR 帧）
        if snap.hr is not None:
            self._hrs.append(float(snap.hr))
            self._n_no_hr += 1 if not snap.hr_ok else 0
            if snap.hr_ok:
                self._n_ok += 1
        elapsed = float(snap.t) - self._t0
        progress = min(1.0, elapsed / self.params["duration"])
        if elapsed >= self.params["duration"]:
            self._finish(elapsed)
        else:
            if int(progress * 100) % 5 == 0:      # 低频刷新（每 ~1.5s 一次）
                self._publish(progress=progress)

    def _finish(self, elapsed: float) -> None:
        self.active = False
        hrs = np.asarray(self._hrs, dtype=np.float64)
        valid_ratio = (self._n_ok / self._n_total) if self._n_total else 0.0
        report: Dict[str, Any] = {
            "started_wall": time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(time.time() - elapsed)),
            "ended_wall": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_s": round(elapsed, 1),
            "fs": self.ctx.fs,
            "hr": {
                "n": int(hrs.size),
                "mean": round(float(np.mean(hrs)), 1) if hrs.size else None,
                "median": round(float(np.median(hrs)), 1) if hrs.size else None,
                "min": round(float(np.min(hrs)), 1) if hrs.size else None,
                "max": round(float(np.max(hrs)), 1) if hrs.size else None,
                "valid_ratio": round(valid_ratio, 3),
                "n_no_hr": int(self._n_no_hr),
            },
            "quality": self.ctx.latest("quality"),
            "hrv_30s": (self.ctx.latest("hrv") or {}).get("windows", {}).get("30s")
            if isinstance(self.ctx.latest("hrv"), dict) else None,
            "arrhythmia": (self.ctx.latest("arrhythmia") or {}).get("recent")
            if isinstance(self.ctx.latest("arrhythmia"), dict) else None,
            "alarm": self.ctx.latest("alarm"),
            "verdict": self._verdict(hrs, valid_ratio),
            "note": "项目仅用于教学演示，不作为医疗诊断用途",
        }
        path = self._write(report)
        self._last_report = report
        self.ctx.log(f"快速测量完成：HR 均值 {report['hr']['mean']} bpm（报告：{path}）")
        self.ctx.emit_event("info",
                            f"30 s 快速测量完成：HR 均值 {report['hr']['mean']} bpm，"
                            f"可信占比 {valid_ratio:.0%}",
                            "quick30_done", path=path)
        self._publish(progress=1.0, report=report)

    def _verdict(self, hrs: np.ndarray, valid_ratio: float) -> str:
        if hrs.size == 0:
            return "无有效心率数据（请检查电极与信号源）"
        if valid_ratio < self.params["min_valid_ratio"]:
            return "数据可信度不足，建议改善信号后重测"
        m = float(np.mean(hrs))
        if m > 100:
            return "本次平均心率偏快（>100 bpm），建议复测确认"
        if m < 50:
            return "本次平均心率偏慢（<50 bpm），建议复测确认"
        return "本次测量在常规范围内（演示级结论，非诊断）"

    def _write(self, report: Dict[str, Any]) -> str:
        """落盘放到后台线程：写盘在采集线程内做会变成漏样（审计 P1）。

        返回预计路径（供日志/事件引用），实际写入异步完成。
        """
        path = os.path.join(self.ctx.out_dir,
                            f"quick30_{time.strftime('%Y%m%d_%H%M%S')}.json")
        out_dir = self.ctx.out_dir
        log = self.ctx.log
        payload = dict(report)

        def _job() -> None:                       # pragma: no cover - 线程
            try:
                os.makedirs(out_dir, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
            except Exception as exc:
                log(f"报告写入失败：{exc!r}")

        threading.Thread(target=_job, daemon=True, name="quick30-write").start()
        return path

    # ---------------------------------------------------------------- 命令
    def on_command(self, action: str, arg: Any = None) -> Any:
        if action == "start":
            return self.start()
        if action == "stop":
            self.stop("command")
            return True
        if action == "status":
            return {"active": self.active, "last": self._last_report}
        return None

    def _publish(self, progress: float, report: Optional[Dict[str, Any]] = None) -> None:
        self.ctx.publish("quick30", {
            "active": self.active,
            "progress": round(progress, 3),
            "duration": self.params["duration"],
            "last_report": report or self._last_report,
        })

    def stats(self) -> Dict[str, Any]:
        st = super().stats()
        st["active"] = self.active
        return st
