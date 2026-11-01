"""intervals.py —— PR / QRS / QT / QTc 自动测量（P1，全 numpy）。

以 R 峰为锚截 ±窗口 → factor 倍线性上采样 + 亚样本对齐 → 多拍中位模板 → 在模板上量。
精度受单样本量化限制（31.25 S/s ≈ 32 ms），precision_ms 必须随结果展示；结果非诊断。
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np

from .base import PluginBase, beats_of, register

_REF = {"pr": (120.0, 200.0), "qrs": (60.0, 110.0),
        "qt": (300.0, 450.0), "qtc": (300.0, 450.0)}


@register
class Intervals(PluginBase):
    name = "intervals"
    priority = 45
    description = "PR/QRS/QT/QTc 自动测量（模板平均，演示级）"

    def __init__(self, factor: int = 8, win_pre: float = 0.30, win_post: float = 0.50,
                 min_beats: int = 4, update_sec: float = 2.5, smooth_n: int = 3,
                 min_r_amp_code: float = 40.0) -> None:
        super().__init__(factor=factor, win_pre=win_pre, win_post=win_post,
                         min_beats=min_beats, update_sec=update_sec,
                         smooth_n=smooth_n, min_r_amp_code=min_r_amp_code)
        self._last_update = 0.0
        self._hist: Deque[Dict[str, Any]] = deque(maxlen=max(3, int(smooth_n)))
        self._latest: Dict[str, Any] = {"ready": False, "reason": "等待数据"}

    # ---------------------------------------------------------------- 数据
    def on_snapshot(self, snap) -> None:
        if float(snap.t) - self._last_update < self.params["update_sec"]:
            return
        b = beats_of(snap)
        wave = snap.wave
        fs = float(snap.fs or 0.0)
        if not b or wave is None or fs <= 0 or len(wave) < 16:
            return
        self._last_update = float(snap.t)

        factor = int(self.params["factor"])
        wave = wave.astype(np.float64)
        n_wave = wave.size
        wave_start = int(snap.n_total) - n_wave          # wave[0] 的绝对样本号
        buf_off = int(b.get("buf_offset") or 0)
        t_s = list(b.get("t_s") or ())

        segs: List[np.ndarray] = []
        for v in t_s:
            rel_f = (buf_off + float(v) * fs) - wave_start     # 亚样本级 R 位置
            c = int(round(rel_f))
            npre = int(np.ceil(self.params["win_pre"] * fs))
            npost = int(np.ceil(self.params["win_post"] * fs))
            if c - npre < 1 or c + npost > n_wave - 1:
                continue
            sl = wave[c - npre: c + npost + 1]
            frac = rel_f - c                                    # [-0.5, 0.5]
            # _up_shift 的语义是"输出在 xi+shift 处取样"；R 位于段内 npre+frac，
            # 要把它搬到 npre 必须取 +frac（取 -frac 会把模板抹开 ±1 个原始样本）
            segs.append(_up_shift(sl, factor, frac))
        if len(segs) < int(self.params["min_beats"]):
            self._latest = {"ready": False,
                            "reason": f"可对齐拍数不足（{len(segs)}<{self.params['min_beats']}）"}
            self.ctx.publish("intervals", self._latest)
            return

        tmpl = np.median(np.vstack(segs), axis=0)
        fs_up = fs * factor
        r_idx = int(round(int(np.ceil(self.params["win_pre"] * fs)) * factor))
        rr_med_s = _median_rr_s(b)
        m = _measure(tmpl, fs_up, r_idx, rr_med_s,
                     min_r_amp=self.params["min_r_amp_code"])
        quant_ms = 1000.0 / fs
        # 诚实降级：单样本量化必须显著小于被测间期，否则宁可不报
        if quant_ms > 40.0:                            # fs < 25 S/s（如板端历史 19.53）
            m.update({"qrs_ms": None, "qt_ms": None, "qtc_ms": None, "reliable": False,
                      "reason": f"采样率 {fs:.1f} S/s 偏低（量化 ±{quant_ms:.0f} ms）："
                                f"QRS/QT 不可测，仅 PR 供参考"})
        m.update({"ready": True, "n_beats": len(segs), "fs_up": round(fs_up, 1),
                  "precision_ms": round(quant_ms, 1), "quant_ms": round(quant_ms, 1),
                  "rr_ms": None if rr_med_s is None else round(rr_med_s * 1000.0, 1),
                  "ref": _REF, "updated_wall": time.strftime("%H:%M:%S"),
                  "note": "演示级测量，非诊断用途；精度随采样率变化，见 precision_ms"})
        self._hist.append(m)
        self._latest = _smooth(self._hist)
        self.ctx.publish("intervals", self._latest)

    def stats(self) -> Dict[str, Any]:
        st = super().stats()
        st["ready"] = bool(self._latest.get("ready"))
        return st


# ----------------------------------------------------------------------------
# 纯函数：上采样 / 测量 / 平滑
# ----------------------------------------------------------------------------
def _up_shift(seg: np.ndarray, factor: int, shift: float) -> np.ndarray:
    """线性上采样 factor 倍，并整体平移 shift 个（原始）样本（亚样本对齐）。"""
    n = seg.size
    x = np.arange(n, dtype=np.float64)
    xi = np.arange(0.0, n - 1.0, 1.0 / factor)
    return np.interp(xi + shift, x, seg)


def _moving_avg(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1 or x.size < n:
        return x
    k = np.ones(n) / n
    return np.convolve(x, k, mode="same")


def _measure(tmpl: np.ndarray, fs_up: float, r_idx: int, rr_s: Optional[float],
             min_r_amp: float = 40.0) -> Dict[str, Any]:
    """在平均模板上测量。返回含 None 的字段表示"没测出来"。"""
    out: Dict[str, Any] = {"pr_ms": None, "qrs_ms": None, "qt_ms": None, "qtc_ms": None,
                           "r_amp_mv": None, "p_found": False, "t_found": False,
                           "reliable": False}
    n = tmpl.size
    edge = max(4, int(0.06 * fs_up))
    base = float(np.median(np.concatenate([tmpl[:edge], tmpl[-edge:]])))
    z = tmpl - base

    # --- R 峰与幅度 ---
    lo, hi = max(0, r_idx - int(0.10 * fs_up)), min(n, r_idx + int(0.10 * fs_up))
    if hi <= lo:
        return out
    seg = z[lo:hi]
    r_i = lo + int(np.argmax(np.abs(seg)))
    r_amp = float(z[r_i])
    out["r_amp_mv"] = round(abs(r_amp) / 65536.0 / 0.301 * 1000.0, 3)
    if abs(r_amp) < min_r_amp:
        out["reason"] = "R 峰幅度过小，无法可靠测量"
        return out
    zz = z * (1.0 if r_amp > 0 else -1.0)          # 极性归一：R 向上

    # --- QRS 起止：差分包络过阈 ---
    env = _moving_avg(np.abs(np.diff(zz, prepend=zz[0])), max(2, int(0.016 * fs_up)))
    w0, w1 = max(0, r_i - int(0.10 * fs_up)), min(n, r_i + int(0.10 * fs_up))
    env_max = float(np.max(env[w0:w1])) if w1 > w0 else 0.0
    if env_max <= 0:
        out["reason"] = "差分包络为空"
        return out
    # 阈值相对"包络本底"取 30%：低阈值会把 QRS 量宽（实测 156 ms → 目标 90~120 ms）
    env_base = float(np.percentile(env, 20))
    thr = env_base + 0.30 * max(env_max - env_base, 1e-6)
    on = r_i
    lim = r_i - int(0.12 * fs_up)
    while on > max(1, lim) and env[on] > thr:
        on -= 1
    off = r_i
    lim2 = r_i + int(0.16 * fs_up)
    while off < min(n - 2, lim2) and env[off] > thr:
        off += 1
    qrs_ms = (off - on) / fs_up * 1000.0
    out["qrs_ms"] = round(qrs_ms, 1) if 40.0 <= qrs_ms <= 220.0 else None

    # --- T 波终末 ---
    t_lo = min(n - 2, off + int(0.04 * fs_up))
    t_hi = min(n - 2, r_i + int(0.45 * fs_up))
    if t_hi > t_lo + 2:
        k = t_lo + int(np.argmax(np.abs(zz[t_lo:t_hi])))
        t_amp = float(zz[k])
        if abs(t_amp) >= 0.08 * abs(r_amp):
            out["t_found"] = True
            j = k
            below = 0
            while j < n - 1 and j < r_i + int(0.55 * fs_up):
                if abs(zz[j]) < 0.15 * abs(t_amp):
                    below += 1
                    if below >= max(3, int(0.008 * fs_up)):
                        break
                else:
                    below = 0
                j += 1
            qt_ms = (j - on) / fs_up * 1000.0
            if 160.0 <= qt_ms <= 650.0:
                out["qt_ms"] = round(qt_ms, 1)
                if rr_s:
                    out["qtc_ms"] = round(qt_ms / float(np.sqrt(max(rr_s, 0.2))), 1)

    # --- P 波起点 → PR ---
    p_lo = max(1, r_i - int(0.28 * fs_up))
    p_hi = max(p_lo + 1, on - int(0.02 * fs_up))
    if p_hi > p_lo + 2:
        k = p_lo + int(np.argmax(np.abs(zz[p_lo:p_hi])))
        p_amp = float(zz[k])
        if abs(p_amp) >= 0.04 * abs(r_amp):
            out["p_found"] = True
            j = k
            while j > p_lo and abs(zz[j]) > 0.15 * abs(p_amp):   # P 起点：15% 幅度过阈
                j -= 1
            pr_ms = (on - j) / fs_up * 1000.0
            if 60.0 <= pr_ms <= 400.0:
                out["pr_ms"] = round(pr_ms, 1)

    vals = [out["pr_ms"], out["qrs_ms"], out["qt_ms"]]
    out["reliable"] = all(v is not None for v in vals)
    if not out["reliable"]:
        miss = [k for k, v in (("PR", out["pr_ms"]), ("QRS", out["qrs_ms"]),
                               ("QT", out["qt_ms"])) if v is None]
        out["reason"] = "未测出：" + "、".join(miss)
    return out


def _median_rr_s(beats: Dict[str, Any]) -> Optional[float]:
    rr = list(beats.get("rr_ms") or ())
    if rr:
        return float(np.median(rr)) / 1000.0
    t_s = list(beats.get("t_s") or ())
    if len(t_s) >= 2:
        d = np.diff(np.asarray(t_s, dtype=np.float64))
        if d.size:
            return float(np.median(d))
    return None


def _smooth(hist: Deque[Dict[str, Any]]) -> Dict[str, Any]:
    """取最近 N 次评估的中位数，压单次抖动。"""
    last = dict(hist[-1])
    for key in ("pr_ms", "qrs_ms", "qt_ms", "qtc_ms", "r_amp_mv"):
        vals = [h.get(key) for h in hist if h.get(key) is not None]
        if vals:
            last[key] = round(float(np.median(vals)), 1)
    last["n_eval"] = len(hist)
    return last
