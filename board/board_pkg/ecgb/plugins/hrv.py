"""hrv.py —— HRV 心率变异性分析（P1）。

按绝对拍号跨快照去重累积 RR，给 30/60/300 s 窗口：时域 SDNN/RMSSD/pNN50；频域用 numpy 自实现
（增量图 4 Hz 重采样 → 去趋势 → Hann → FFT → LF/HF）。频域需 ≥ fd_min_span_s 秒，否则 ready=False。
"""
from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np

from .base import PluginBase, beats_of, register


@register
class Hrv(PluginBase):
    name = "hrv"
    priority = 30
    description = "HRV：时域 SDNN/RMSSD/pNN50 + 频域 LF/HF（演示级）"

    def __init__(self, windows: str = "30,60,300", update_sec: float = 2.0,
                 min_rr_ms: float = 250.0, max_rr_ms: float = 2000.0,
                 miss_factor: float = 1.6, min_rr_for_td: int = 8,
                 fd_min_span_s: float = 50.0, fs_resample: float = 4.0) -> None:
        super().__init__(windows=windows, update_sec=update_sec, min_rr_ms=min_rr_ms,
                         max_rr_ms=max_rr_ms, miss_factor=miss_factor,
                         min_rr_for_td=min_rr_for_td, fd_min_span_s=fd_min_span_s,
                         fs_resample=fs_resample)
        self._win_list: List[int] = [int(s) for s in str(windows).split(",") if s.strip()]
        self._rr: Deque[Tuple[int, float]] = deque(maxlen=4096)   # (绝对拍号, RR ms)
        self._last_idx: Optional[int] = None
        self._last_update = 0.0
        self._n_corrected = 0
        self._latest: Dict[str, Any] = {}

    # ---------------------------------------------------------------- 数据
    def on_snapshot(self, snap) -> None:
        b = beats_of(snap)
        if not b:
            return
        idx = list(b.get("idx_abs") or ())
        t_s = list(b.get("t_s") or ())
        if len(idx) < 2 or len(t_s) != len(idx):
            return
        n_new = 0
        for k in range(1, len(idx)):
            a = int(idx[k])
            if self._last_idx is not None and a <= self._last_idx:
                continue
            rr = (float(t_s[k]) - float(t_s[k - 1])) * 1000.0
            if self.params["min_rr_ms"] <= rr <= self.params["max_rr_ms"]:
                self._rr.append((a, rr))
                n_new += 1
        if idx:
            self._last_idx = int(idx[-1])
        if n_new:
            self._maybe_update(snap)

    def _maybe_update(self, snap) -> None:
        if float(snap.t) - self._last_update < self.params["update_sec"]:
            return
        self._last_update = float(snap.t)
        self._compute()

    # ---------------------------------------------------------------- 计算
    def _compute(self) -> None:
        rrs_all = [rr for _, rr in self._rr]
        out: Dict[str, Any] = {
            "updated_t": float(self._last_update),
            "n_rr_total": len(rrs_all),
            "n_corrected_total": self._n_corrected,
            "windows": {},
            "note": "演示级统计，非诊断用途；频域需 ≥%d s 数据" % int(self.params["fd_min_span_s"]),
        }
        for w in self._win_list:
            sel = _tail_by_span(self._rr, float(w) * 1000.0)
            out["windows"][f"{w}s"] = self._window_stats(sel, w)
        self._latest = out
        self.ctx.publish("hrv", out)

    def _window_stats(self, sel: List[Tuple[int, float]], w: int) -> Dict[str, Any]:
        p = self.params
        if len(sel) < p["min_rr_for_td"]:
            return {"ready": False, "n_rr": len(sel),
                    "reason": f"RR 样本不足（{len(sel)}<{p['min_rr_for_td']}）"}
        rr = np.asarray([v for _, v in sel], dtype=np.float64)
        span_s = float(rr.sum()) / 1000.0

        # 单遍 k 倍漏搏修正：>miss_factor×中位数 的 RR 视作 k 拍
        med = float(np.median(rr))
        k = np.maximum(1, np.rint(rr / max(med, 1e-9))).astype(int)
        k = np.where(rr > p["miss_factor"] * med, k, 1)
        n_corr = int(np.sum(k > 1))
        rr_c = rr / np.maximum(k, 1)

        td = {
            "ready": True,
            "n_rr": int(rr_c.size),
            "span_s": round(span_s, 1),
            "n_corrected": n_corr,
            "mean_hr": round(float(60000.0 / np.mean(rr_c)), 1),
            "sdnn_ms": round(float(np.std(rr_c, ddof=1)), 1),
            "rmssd_ms": round(float(np.sqrt(np.mean(np.diff(rr_c) ** 2))), 1)
            if rr_c.size > 1 else None,
            "pnn50": round(float(np.mean(np.abs(np.diff(rr_c)) > 50.0)), 3)
            if rr_c.size > 1 else None,
        }
        if span_s < p["fd_min_span_s"]:
            td.update({"freq_ready": False,
                       "freq_reason": f"窗口 {span_s:.0f} s < {p['fd_min_span_s']:.0f} s，"
                                      f"LF 频段无法可靠估计"})
            return td
        td.update(self._freq(rr_c, span_s))
        td["freq_ready"] = True
        return td

    def _freq(self, rr_c: np.ndarray, span_s: float) -> Dict[str, Any]:
        """numpy 自实现：增量图重采样 → 去线性趋势 → Hann → FFT → 频带积分。"""
        fs_r = float(self.params["fs_resample"])
        t_end = np.cumsum(rr_c) / 1000.0                    # 每拍结束时刻（秒）
        t_end = t_end - t_end[0]
        if t_end[-1] < 1.0:
            return {"freq_ready": False, "freq_reason": "有效时长不足 1 s"}
        if float(np.max(rr_c) - np.min(rr_c)) < 1e-9:        # 全等 RR：频域无意义
            return {"freq_ready": False, "freq_reason": "RR 恒定，无可分析的变异"}
        grid = np.arange(0.0, t_end[-1], 1.0 / fs_r)
        if grid.size < 32:                                   # 退化保护（审计 P2）
            return {"freq_ready": False, "freq_reason": "有效时长不足（重采样点过少）"}
        y = np.interp(grid, t_end, rr_c)
        try:
            coef = np.polyfit(grid, y, 1)                    # 去线性趋势（消 VLF 漂移）
            y = y - np.polyval(coef, grid)
        except Exception:
            y = y - float(np.mean(y))
        n = y.size
        win = np.hanning(n)
        yw = (y - np.mean(y)) * win
        spec = np.fft.rfft(yw)
        psd = (np.abs(spec) ** 2) * 2.0 / (fs_r * float(np.sum(win ** 2)))
        freqs = np.fft.rfftfreq(n, d=1.0 / fs_r)
        df = freqs[1] - freqs[0] if freqs.size > 1 else 1.0

        def band(lo: float, hi: float) -> float:
            m = (freqs >= lo) & (freqs < hi)
            return float(np.sum(psd[m]) * df)

        lf = band(0.04, 0.15)
        hf = band(0.15, 0.40)
        return {
            "lf_ms2": round(lf, 1),
            "hf_ms2": round(hf, 1),
            "lf_hf": round(lf / hf, 3) if hf > 1e-9 else None,
            "total_ms2": round(lf + hf, 1),
            "freq_span_s": round(span_s, 1),
        }

    def stats(self) -> Dict[str, Any]:
        st = super().stats()
        st["n_rr_total"] = len(self._rr)
        return st


def _tail_by_span(rr: Deque[Tuple[int, float]], span_ms: float) -> List[Tuple[int, float]]:
    """从最新往回取，累计时长不超过 span_ms 的一段 RR。"""
    out: List[Tuple[int, float]] = []
    acc = 0.0
    for item in reversed(rr):
        acc += item[1]
        if acc > span_ms:
            break
        out.append(item)
    out.reverse()
    return out
