"""hr_alarm.py —— 心率超限报警（P0，零接口改动）。

分级对齐 IEC 60601-1-8：high = 心率 ≥150 或 ≤35；medium = 越 hr_hi/hr_lo 且持续 sustain 秒；
info = 恢复。进入需持续 N 秒、退出需回到阈值内侧 hysteresis bpm；只用 snap.hr / snap.hr_ok。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .base import PluginBase, register


@register
class HrAlarm(PluginBase):
    name = "hr_alarm"
    priority = 20
    description = "心率超限报警（高/中/低分级 + 迟滞防抖）"

    def __init__(self, hr_hi: float = 120.0, hr_lo: float = 50.0,
                 hi_extreme: float = 150.0, lo_extreme: float = 35.0,
                 sustain: float = 3.0, sustain_extreme: float = 1.0,
                 hysteresis: float = 3.0, unreliable_hold: float = 2.0,
                 require_ok: bool = True) -> None:
        super().__init__(hr_hi=hr_hi, hr_lo=hr_lo, hi_extreme=hi_extreme,
                         lo_extreme=lo_extreme, sustain=sustain,
                         sustain_extreme=sustain_extreme,
                         hysteresis=hysteresis, unreliable_hold=unreliable_hold,
                         require_ok=require_ok)
        self._state = "等待数据"
        self._level = "info"
        self._cand = ""            # 正在累积的候选状态
        self._cand_since: Optional[float] = None
        self._active_since: Optional[float] = None
        self._bad_since: Optional[float] = None      # 不可信起点（防单帧抖动）
        self._latest: Dict[str, Any] = {}

    # ---------------------------------------------------------------- 主逻辑
    def on_snapshot(self, snap) -> None:
        hr = snap.hr
        ok = bool(snap.hr_ok)
        now = float(snap.t or 0.0)

        if hr is None or (self.params["require_ok"] and not ok):
            self._cand, self._cand_since = "", None
            if self._bad_since is None:
                self._bad_since = now
            if self._state not in ("等待数据", "心率不可信") and \
                    (now - self._bad_since) >= self.params["unreliable_hold"]:
                self._set_state("心率不可信", "low", now, hr)
                self.ctx.emit_event("low", "心率暂不可信（信号质量不足），报警暂停", "hr_unreliable")
            self._publish(hr, now)
            return
        self._bad_since = None                           # 恢复可信：清零不可信计时

        hr = float(hr)
        p = self.params
        cand = ""
        if hr >= p["hi_extreme"] or hr <= p["lo_extreme"]:
            cand = "心率危急"
        elif hr >= p["hr_hi"]:
            cand = "心动过速"
        elif hr <= p["hr_lo"]:
            cand = "心动过缓"

        if cand:
            if cand != self._cand:
                self._cand, self._cand_since = cand, now
            dur = now - (now if self._cand_since is None else self._cand_since)
            need = p["sustain_extreme"] if cand == "心率危急" else p["sustain"]
            if dur >= need and self._state != cand:
                level = "high" if cand == "心率危急" else "medium"
                self._set_state(cand, level, now, hr)
                code = {"心动过速": "tachy", "心动过缓": "brady", "心率危急": "hr_extreme"}[cand]
                self.ctx.emit_event(level, f"{cand}：心率 {hr:.0f} bpm（持续 {dur:.1f} s）",
                                    code, hr=round(hr, 1))
        else:
            self._cand, self._cand_since = "", None
            # 迟滞恢复
            recovered = (self._state in ("心动过速", "心率危急") and hr < p["hr_hi"] - p["hysteresis"]) or \
                        (self._state in ("心动过缓", "心率危急") and hr > p["hr_lo"] + p["hysteresis"]) or \
                        (self._state == "心率不可信")
            if recovered:
                dur = now - (now if self._active_since is None else self._active_since)
                prev = self._state
                self._set_state("正常", "info", now, hr)
                self.ctx.emit_event("info", f"心率恢复正常：{hr:.0f} bpm（此前 {prev} {dur:.1f} s）",
                                    "hr_recovered", hr=round(hr, 1))
            elif self._state in ("等待数据",):
                self._set_state("正常", "info", now, hr)
        self._publish(hr, now)

    # ---------------------------------------------------------------- 工具
    def _set_state(self, state: str, level: str, now: float, hr: Optional[float]) -> None:
        self._state, self._level = state, level
        self._active_since = now

    def _publish(self, hr: Optional[float], now: float) -> None:
        self._latest = {
            "state": self._state,
            "level": self._level,
            "hr": None if hr is None else round(float(hr), 1),
            "sustain": 0.0 if self._cand_since is None else round(now - self._cand_since, 1),
            "candidate": self._cand,
            "active_since": self._active_since,
            "limits": {"hi": self.params["hr_hi"], "lo": self.params["hr_lo"]},
        }
        self.ctx.publish("alarm", self._latest)

    def stats(self) -> Dict[str, Any]:
        st = super().stats()
        st.update({"state": self._state, "level": self._level})
        return st
