"""quality.py —— 信号质量 SQI 与电极状态监测（P0，零接口改动）。

从 snap.wave / snap.extra 算 SQI 并输出"良好/劣化/脱落"（软件判定：AD8232 的 LOD± 未接板，
用饱和占比 + 平坦占比 + 幅度替代）。只读快照；单帧预算 ≤ 5 ms；无 scipy。
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np

from .base import PluginBase, register


@register
class SignalQuality(PluginBase):
    name = "quality"
    priority = 10          # 先于其它插件：给网页/报警提供质量背景
    description = "信号质量 SQI 与电极状态（良好/劣化/脱落）"

    def __init__(self, win_sec: float = 1.5, flat_thr: float = 0.995,
                 flat_knee_lo: float = 0.75, flat_knee_hi: float = 0.95,
                 sat_thr: float = 0.05, sqi_degraded: float = 0.45,
                 hold_sec: float = 1.5, min_amp_code: float = 60.0,
                 drop_rate_bad: float = 2.0) -> None:
        super().__init__(win_sec=win_sec, flat_thr=flat_thr,
                         flat_knee_lo=flat_knee_lo, flat_knee_hi=flat_knee_hi,
                         sat_thr=sat_thr,
                         sqi_degraded=sqi_degraded, hold_sec=hold_sec,
                         min_amp_code=min_amp_code, drop_rate_bad=drop_rate_bad)
        self._state = "未知"
        self._state_since = 0.0
        self._cand = ""
        self._cand_since = 0.0
        self._last_dropped: Dict[str, Any] = {}
        self._latest: Dict[str, Any] = {}

    # ---------------------------------------------------------------- 主逻辑
    def on_snapshot(self, snap) -> None:
        wave = snap.wave
        fs = float(snap.fs or self.ctx.fs or 0.0)
        if wave is None or len(wave) < 8 or fs <= 0:
            return
        k = max(8, int(self.params["win_sec"] * fs))
        seg = wave[-k:] if len(wave) > k else wave
        x = seg.astype(np.float32)

        # ---- 三类原始特征 ----
        # 平坦度必须按"时间块"判，不能看相邻样本差：高采样率下正常心电的相邻差本来就可以很小，
        # 逐点判会把好信号冤枉成平坦。
        block = max(2, min(int(round(0.05 * fs)), 8))               # 50 ms/块，钳位 2~8 样本
        m = x.size // block
        if m >= 3:
            blocks = x[:m * block].reshape(m, block).mean(axis=1)
            d = np.abs(np.diff(blocks))
            flat_ratio = float(np.mean(d <= 1.0))                   # 贴平（无变化）
        else:
            flat_ratio = 0.0                                        # 窗口太短：不判平坦
            d = np.zeros(1, dtype=np.float32)
        sat_ratio = float(np.mean((x <= 8.0) | (x >= 65528.0)))     # 贴轨（饱和）
        p5, p95 = np.percentile(x, [5.0, 95.0])
        pp = float(p95 - p5)                                        # 峰峰（码值）

        # ---- 采样健康度（来自 extra，插件不自己造口径）----
        extra = snap.extra or {}
        est_fs = float(extra.get("est_fs_sw") or fs)
        rate_ratio = est_fs / fs if fs > 0 else 1.0

        n_hw = int(snap.n_hw or 0)
        prev = self._last_dropped.get("n_hw")
        drop_rate = 0.0
        if prev is not None and snap.t > self._last_dropped.get("t", snap.t):
            dt = max(1e-6, snap.t - self._last_dropped["t"])
            d_drop = max(0, int(snap.dropped or 0) - int(self._last_dropped.get("dropped", 0)))
            drop_rate = d_drop / dt
        self._last_dropped = {"n_hw": n_hw, "dropped": int(snap.dropped or 0), "t": snap.t}

        # ---- 分项打分（都是 0~1，越差越小）----
        # 平坦度用"拐点式"打分：安静段（ST/基线）本来就有小变化，只有接近全平时才扣分，
        # 否则会周期性误报"质量劣化"。
        q_amp = _clip01((pp - self.params["min_amp_code"]) / 300.0)      # 幅度够不够
        lo_k, hi_k = self.params["flat_knee_lo"], self.params["flat_knee_hi"]
        q_flat = _clip01((hi_k - flat_ratio) / max(hi_k - lo_k, 1e-6))
        q_sat = _clip01(1.0 - sat_ratio / self.params["sat_thr"])         # 贴轨=差
        q_rate = _clip01(1.0 - abs(1.0 - rate_ratio) / 0.5)               # 有效采样率漂移
        q_drop = _clip01(1.0 - drop_rate / self.params["drop_rate_bad"])  # 漏样速率
        sqi = float(min(q_amp, q_flat, q_sat) ** 0.7 * (q_rate * q_drop) ** 0.15)

        # ---- 状态判定（带方向性防抖）----
        if sat_ratio >= 0.60 or flat_ratio >= self.params["flat_thr"]:
            raw_state = "脱落"
        elif sqi < self.params["sqi_degraded"] or q_amp < 0.2:
            raw_state = "劣化"
        else:
            raw_state = "良好"
        if raw_state != self._state:
            if raw_state != self._cand:                     # 新的候选：重新计时
                self._cand, self._cand_since = raw_state, snap.t
            pending = snap.t - self._cand_since
            # 变差要"站得住"才认（hold_sec）；恢复立即生效（宁可早报好）
            if self._state == "未知" or raw_state == "良好" or pending >= self.params["hold_sec"]:
                self._state, self._state_since = raw_state, snap.t
                self._cand = ""
                self._emit_state(sqi, pp, flat_ratio, sat_ratio, drop_rate)
        else:
            self._cand = ""

        self._latest = {
            "sqi": round(sqi, 3),
            "electrode": self._state,
            "pp_code": round(pp, 1),
            "flat_ratio": round(flat_ratio, 4),
            "sat_ratio": round(sat_ratio, 4),
            "rate_ratio": round(rate_ratio, 3),
            "drop_rate": round(drop_rate, 2),
            "sub": {"amp": round(q_amp, 3), "flat": round(q_flat, 3),
                    "sat": round(q_sat, 3), "rate": round(q_rate, 3),
                    "drop": round(q_drop, 3)},
        }
        self.ctx.publish("quality", self._latest)

    # ---------------------------------------------------------------- 事件
    def _emit_state(self, sqi: float, pp: float, flat: float,
                    sat: float, drop_rate: float) -> None:
        if self._state == "脱落":
            self.ctx.emit_event("high", "电极脱落或信号贴轨（请检查电极/导线）",
                                "lead_off", sqi=round(sqi, 3), sat=round(sat, 3))
        elif self._state == "劣化":
            self.ctx.emit_event("medium", f"信号质量劣化（SQI {sqi:.2f}，峰峰 {pp:.0f} 码）",
                                "quality_low", sqi=round(sqi, 3))
        elif self._state == "良好":
            self.ctx.emit_event("info", "信号质量恢复良好", "quality_ok", sqi=round(sqi, 3))

    def on_close(self) -> None:
        self.ctx.publish("quality", self._latest)

    def stats(self) -> Dict[str, Any]:
        st = super().stats()
        st.update({"sqi": self._latest.get("sqi"), "electrode": self._state})
        return st


def _clip01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else float(v))
