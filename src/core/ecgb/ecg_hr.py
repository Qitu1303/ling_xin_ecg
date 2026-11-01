"""ecg_hr.py —— 心率检测（QRS / R 峰）与可信度判定。

低采样率（19.53~31.25 S/s）下先插值上采样到 >=200 Hz：带通 → 能量包络 → 自相关估周期 →
逐拍接链 → 子样本精定位 → RR 统计；只依赖 numpy。ok=False 时 hr_bpm 恒为 None（"HR --"）。
"""

from __future__ import annotations

import dataclasses
import math
import time

import numpy as np

# regs.py 是寄存器口径的冻结件、唯一事实来源；同时容忍"不作为包导入"的离线用法。
try:
    from .regs import CODE_MID
except ImportError:                       # 板上 pynq 环境常常是平铺 import
    try:
        from regs import CODE_MID
    except ImportError:                   # 兜底：与 regs 保持一致
        CODE_MID = 32768.0

__all__ = ["HrResult", "HrDetector"]


@dataclasses.dataclass(frozen=True)
class HrResult:
    """一次心率评估的结果（字段与 docs/接口表_冻结版.md §3 一致，不得增减语义）。

    hr_bpm: 心率，**ok=False 时恒为 None**；quality: 0~1 质量分；reason: 判定依据。
    beat_idx / beat_t_s: 相对缓冲起点的原始样本号 / 秒值（含子样本修正）；
    rr_ms: 漏搏修正后的 RR（ms，长度可能与 len(beat_t_s)-1 不同，勿按下标配对）；
    buf_offset: 缓冲起点绝对样本号，第 k 拍绝对样本号 = buf_offset + beat_idx[k]。
    """

    hr_bpm: float | None
    n_beats: int
    ok: bool
    quality: float
    reason: str
    beat_idx: tuple = ()
    beat_t_s: tuple = ()
    rr_ms: tuple = ()
    buf_offset: int = 0

    def __str__(self) -> str:
        hr = "--" if self.hr_bpm is None else "%.1f" % self.hr_bpm
        return "HrResult(hr=%s bpm, beats=%d, ok=%s, q=%.2f, %s)" % (
            hr, self.n_beats, self.ok, self.quality, self.reason)


# --------------------------------------------------------------------------
# 纯 numpy 小工具（不依赖 scipy）
# --------------------------------------------------------------------------

def _clip01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else float(v))


def _movavg_same(x: np.ndarray, n: int) -> np.ndarray:
    """长度不变的移动平均（边界用反射填充，避免两端被拉平产生假峰）。

    用 cumsum 实现：板上要按帧反复重算，O(N) 才够便宜。
    """
    x = np.asarray(x, dtype=np.float64)
    m = x.size
    if n <= 1 or m <= 1:
        return x.copy()
    n = int(min(n, m))
    left = n // 2
    right = n - 1 - left
    xp = np.pad(x, (left, right), mode="reflect")
    c = np.concatenate(([0.0], np.cumsum(xp, dtype=np.float64)))
    return (c[n:] - c[:-n]) / float(n)


def _upsample(x: np.ndarray, fs: float, target_hz: float):
    """线性插值上采样到 >= target_hz，返回 (信号, 新采样率)。

    取 fs 的**整数倍**，让"上采样域索引 / factor = 原样本索引"精确成立，换算回原始样本号
    时没有累积取整误差。用线性而非高阶插值：每搏只有 2~4 点时高阶插值会过冲造出假峰。
    """
    factor = int(max(1, math.ceil(float(target_hz) / float(fs))))
    fs_up = float(fs) * factor
    if factor == 1 or x.size < 2:
        return x.astype(np.float64, copy=True), fs_up
    n = x.size
    t_new = np.arange((n - 1) * factor + 1, dtype=np.float64) / factor
    return np.interp(t_new, np.arange(n, dtype=np.float64), x), fs_up


def _bandpass(x: np.ndarray, fs_up: float, hp_sec: float, lp_sec: float) -> np.ndarray:
    """极简 FIR 带通：先 (x - MA(hp_sec)) 做高通，再 MA(lp_sec) 做低通。

    板上没有 scipy；MA 是零依赖 + 严格线性相位 + O(N) 的选择，线性相位保证 R 峰不被拖出
    与心率相关的时延。MA(0.2 s) 在 5 Hz 处为零、对 0.3 Hz 基线漂移衰减 40 dB 以上；
    MA(0.04 s) 在 25 Hz 处为零，压掉高频噪声（噪声的微分会放大高频）。
    """
    y = x - _movavg_same(x, int(round(hp_sec * fs_up)))
    return _movavg_same(y, int(round(lp_sec * fs_up)))


def _local_maxima(a: np.ndarray, r: int) -> np.ndarray:
    """返回 a 中的局部极大下标（半径 r 邻域内最大；等值平台全算，后续靠不应期收敛）。"""
    n = a.size
    if n == 0:
        return np.empty(0, dtype=np.int64)
    r = int(max(1, min(r, max(1, (n - 1) // 2))))
    if n <= 2 * r:
        return np.asarray([int(np.argmax(a))], dtype=np.int64)
    try:
        from numpy.lib.stride_tricks import sliding_window_view
        w = sliding_window_view(np.asarray(a, dtype=np.float64), 2 * r + 1)
        mx = w.max(axis=1)
        idx = np.nonzero(a[r:n - r] >= mx)[0] + r
    except Exception:                      # 老 numpy 兜底（板上 numpy 版本不可控）
        keep = []
        for i in range(r, n - r):
            if a[i] >= a[i - r:i + r + 1].max():
                keep.append(i)
        idx = np.asarray(keep, dtype=np.int64)
    return idx.astype(np.int64)


def _apply_refractory(idx: np.ndarray, score: np.ndarray, fs_up: float,
                      refractory: float) -> np.ndarray:
    """时间顺序贪心 + 不应期：两个候选靠得太近时保留能量大的那个。

    不应期 >= 0.25 s 是生理约束（心室肌有效不应期）。替换后可能与前一个候选再次冲突，
    所以往前回溯，保证输出里任意两点间隔都达标。
    """
    gap = max(1.0, float(refractory) * fs_up)
    out = []
    for i in idx:
        i = int(i)
        if out and (i - out[-1]) < gap:
            if score[i] > score[out[-1]]:
                out[-1] = i
                while len(out) >= 2 and (out[-1] - out[-2]) < gap:
                    if score[out[-1]] > score[out[-2]]:
                        out.pop(-2)
                    else:
                        out.pop(-1)
        else:
            out.append(i)
    return np.asarray(out, dtype=np.int64)


def _acf_periods(env: np.ndarray, fs_up: float, t_min: float, t_max: float,
                 min_acf: float = 0.20, max_n: int = 4):
    """包络自相关的显著周期候选（秒，升序）；没有显著周期返回空列表。

    门限 min_acf 刻意偏低（150 bpm + 10% 噪声时真周期处只有 0.255，而"有没有信号"由后面的
    节律规整度硬门槛兜底，不靠这一道）。包络把 QRS 压成稀疏大峰，自相关峰又尖又稳。
    """
    n = int(env.size)
    if n < 8:
        return []
    # 截顶（winsorize）：运动伪迹的能量可以比 QRS 高几十倍，直接算自相关会让零延迟项被伪迹
    # 独占、真周期被稀释到看不见；截到 8 倍噪声地板后 QRS 的周期结构仍在。
    clip = 8.0 * float(np.median(env)) + 1e-12
    x = np.minimum(env, clip)
    x = x - float(x.mean())
    nfft = 1
    while nfft < 2 * n:
        nfft <<= 1
    X = np.fft.rfft(x, nfft)
    ac = np.fft.irfft(X * np.conj(X), nfft)[:n]
    if not (float(ac[0]) > 0.0):
        return []
    ac = ac / float(ac[0])
    i0 = max(1, int(round(t_min * fs_up)))
    i1 = min(n - 1, int(round(t_max * fs_up)))
    if i1 - i0 < 3:
        return []
    seg = ac[i0:i1 + 1]
    pk = _local_maxima(seg, max(1, int(round(0.02 * fs_up)))) + i0
    if pk.size == 0:
        return []
    amax = float(ac[pk].max())
    if amax < min_acf:
        return []
    strong = pk[ac[pk] >= max(min_acf, 0.5 * amax)]
    if strong.size > max_n:                # 太多时留最强的几个（真周期通常就在其中）
        strong = strong[np.argsort(ac[strong])[::-1][:max_n]]
    strong = np.sort(strong)
    # 不在这里就断定哪个是基周期：漏搏会让 2T 的自相关也很强，
    # 到底该用哪个，交给 _detect 里"哪条链能凑出更规整的节律"来定（多假设选优）。
    return [float(int(l)) / float(fs_up) for l in strong]


def _chain_peaks(cand: np.ndarray, amps: np.ndarray, fs_up: float, T0: float,
                 low_thr: float, max_tracks: int = 16) -> np.ndarray:
    """按"上一搏的 RR"逐拍接链，返回被接受的候选下标（已按时间排序）。

    从幅值最大的空闲候选起锚；下一搏取 k*T0 预测位置 ±0.35*T0 内、离预测最近且幅值
    >= low_thr 的候选。k 取 1/2/3：k>1 即漏搏 —— **位置由周期给出，幅值只需过噪声地板**，
    这正是对付"R 峰幅度被采样相位调制成 10 倍"的关键。接不动就从下一个强候选重起链，
    只保留搏数 >= 3 的链。
    """
    cand = np.asarray(cand, dtype=np.int64)
    amps = np.asarray(amps, dtype=np.float64)
    if cand.size == 0:
        return cand
    order = np.argsort(cand)
    idx = cand[order]
    amp = amps[order]
    n = int(idx.size)
    taken = np.zeros(n, dtype=bool)
    win = 0.35 * float(T0) * fs_up
    step = float(T0) * fs_up
    tracks = []
    for _ in range(int(max_tracks)):
        free = np.nonzero(~taken)[0]
        if free.size == 0:
            break
        a = int(free[int(np.argmax(amp[free]))])
        if amp[a] < low_thr and tracks:
            break
        taken[a] = True
        track = [a]
        for direction in (1, -1):
            cur = a
            while True:
                nxt = None
                for kk in (1, 2, 3):
                    pred = float(idx[cur]) + direction * kk * step
                    lo = int(np.searchsorted(idx, pred - win, side="left"))
                    hi = int(np.searchsorted(idx, pred + win, side="right"))
                    if hi <= lo:
                        continue
                    seg = np.arange(lo, hi)
                    ok = (~taken[seg]) & (amp[seg] >= low_thr)
                    if not ok.any():
                        continue
                    seg = seg[ok]
                    # 幅值 x 靠近预测位置的加权：真搏通常又强又准；
                    # 只看"离预测最近"会被恰好落在预测位置上的噪声峰骗走。
                    dt = np.abs(idx[seg].astype(np.float64) - pred)
                    wt = np.exp(-0.5 * (dt / (0.15 * step)) ** 2)
                    nxt = int(seg[int(np.argmax(amp[seg] * wt))])
                    break
                if nxt is None:
                    break
                taken[nxt] = True
                if direction == 1:
                    track.append(nxt)
                else:
                    track.insert(0, nxt)
                cur = nxt
        tracks.append(track)
        # **关键**：把这条链已解释的搏附近 (±0.75T) 的空闲候选屏蔽掉。只屏蔽一小圈的话，下一条链
        # 会从半周期处起锚，接出与主链交错的串，RR 里混进 0.5T 假间隔（40 bpm 报成 80 bpm）。
        # 取 0.75T 后相邻搏的屏蔽区首尾相接；真正的漏搏由链内的 k=2/3 跳步负责补。
        blank = 0.75 * float(T0) * fs_up
        for i in track:
            lo_b = int(np.searchsorted(idx, idx[i] - blank, side="left"))
            hi_b = int(np.searchsorted(idx, idx[i] + blank, side="right"))
            taken[lo_b:hi_b] = True
    keep = [t for t in tracks if len(t) >= 3]
    if not keep:
        keep = [max(tracks, key=len)] if tracks else []
    out = sorted(i for t in keep for i in t)
    return np.asarray([int(idx[i]) for i in out], dtype=np.int64)


def _dedup_mask(t: np.ndarray, min_gap: float) -> np.ndarray:
    """返回"按时间顺序去重后要保留"的布尔掩码：间隔小于 min_gap 的只留先到的一个。

    接出的弱搏在包络上位置有偏差，|y| 精定位可能把它吸附到邻近强 R 峰，一拍变两拍。
    返回掩码而非数组，便于调用方把同一掩码套回包络下标数组。
    """
    t = np.asarray(t, dtype=np.float64)
    m = np.ones(t.size, dtype=bool)
    if t.size < 2:
        return m
    last = 0
    for i in range(1, int(t.size)):
        if t[i] - t[last] >= min_gap:
            last = i
        else:
            m[i] = False
    return m


def _refine_peaks(y: np.ndarray, sel: np.ndarray, fs_up: float,
                  lead: float = 0.10, lag: float = 0.06) -> np.ndarray:
    """把包络峰（有积分时延）回落到带通信号 |y| 的真实 R 峰，并做抛物线子样本插值。

    子样本插值把上采样后的毫秒级量化误差再压一个量级。
    """
    a0 = int(round(lead * fs_up))
    b0 = int(round(lag * fs_up))
    ay = np.abs(np.asarray(y, dtype=np.float64))
    out = np.empty(sel.size, dtype=np.float64)
    for k, p in enumerate(sel):
        p = int(p)
        lo = max(0, p - a0)
        hi = min(ay.size, p + b0 + 1)
        if hi - lo < 3:
            out[k] = p / fs_up
            continue
        j = int(np.argmax(ay[lo:hi])) + lo
        delta = 0.0
        if 0 < j < ay.size - 1:
            y0, y1, y2 = float(ay[j - 1]), float(ay[j]), float(ay[j + 1])
            den = y0 - 2.0 * y1 + y2
            if abs(den) > 1e-12:
                delta = max(-0.5, min(0.5, 0.5 * (y0 - y2) / den))
        out[k] = (j + delta) / fs_up
    return out


def _template_quality(y: np.ndarray, peaks: np.ndarray, fs_up: float,
                      pre: float = 0.08, post: float = 0.10) -> float:
    """逐搏形态一致性：各搏窗口归一化后与"中位模板"的相关系数中位数（0~1）。

    真 QRS 逐搏形态高度相似，噪声/伪迹混出的伪峰形态忽高忽低。
    **极性归一必须先做**：QRS 双相，低采样率下部分搏窗口由 S 波主导，不翻正相关系数会在
    ±0.9 之间跳（实测中位相关 -0.72 → 质量分直接 0）。按窗口中心样点的符号翻正。
    """
    w1 = int(round(pre * fs_up))
    w2 = int(round(post * fs_up))
    segs = []
    for p in peaks:
        a, b = int(p) - w1, int(p) + w2
        if a < 0 or b > y.size:
            continue
        s = np.asarray(y[a:b], dtype=np.float64)
        if float(s[w1]) < 0.0:             # 中心样点为负 → 这一搏由 S 波主导，翻正
            s = -s
        s = s - s.mean()
        sd = float(s.std())
        if sd < 1e-9:
            continue
        segs.append(s / sd)
    if not segs:
        return 0.0
    if len(segs) < 3:
        return 0.3                        # 只有 1~2 搏，样本太少，给中性偏低分
    M = np.vstack(segs)
    tmpl = np.median(M, axis=0)
    nt = float(np.linalg.norm(tmpl))
    if nt < 1e-12:
        return 0.0
    tmpl = tmpl / nt
    corr = (M @ tmpl) / (np.linalg.norm(M, axis=1) + 1e-12)
    return _clip01((float(np.median(corr)) - 0.25) / 0.6)


def _kcorrect(vals, base):
    """把"≈n 倍基准周期"的间隔按 n 段摊平（n 封顶 4），返回 (修正后序列, 被判为漏搏的段数)。

    不修正的话，一次漏跳就让 bpm 直接腰斩。
    """
    out = []
    n_miss = 0
    for v in vals:
        k = int(round(float(v) / (base + 1e-12)))
        k = 1 if k < 1 else (4 if k > 4 else k)
        if k > 1:
            n_miss += 1
        out.extend([float(v) / k] * k)
    return np.asarray(out, dtype=np.float64), n_miss


def _rr_stats(t_ref: np.ndarray, refractory: float):
    """由 R 峰时刻算 RR 统计量，返回 dict：
    rr / rr_c / rr_med / cv / inl / bpm（中位数口径）/ bpm_mean（均值口径）。

    中位数而非均值：单次 RR 带 ±1 个原始样本的采样相位抖动；
    cv = 1.4826*MAD/中位数，对离群 RR 稳健，用来判"是不是规整节律"。
    """
    rr = np.diff(np.asarray(t_ref, dtype=np.float64))
    if rr.size < 1:
        return {"rr": rr, "rr_c": rr, "rr_med": 0.0, "cv": 1e9, "inl": 0.0,
                "miss_frac": 1.0, "bpm": 0.0, "bpm_mean": 0.0}
    med0 = float(np.median(rr))
    # 下限必须挂在不应期上，不能写死 0.35 s：150 bpm 在 19.53 S/s 下 RR 只有 7~8 个样本
    # （0.36~0.41 s），写死会把短 RR 当野值剔掉，中位数被抬高、bpm 系统性偏低。
    good = (rr > 0.7 * float(refractory)) & (rr < 2.6 * med0)
    rr_in = rr[good] if int(good.sum()) >= 2 else rr
    med = float(np.median(rr_in))
    # 判"这个间隔是几拍"的基准周期取 35 分位而非中位数：漏搏把分布往大处拉，中位数会把 3 拍
    # 的缺口算成 2 拍；另一头用不应期兜住，避免基准过小把正常间隔误当漏搏分开。
    base = max(float(np.percentile(rr_in, 35.0)), float(refractory))
    rr_c, n_miss = _kcorrect(rr_in, base)
    # 用中位数而非均值：漏搏修正后的序列仍会有个别被吸附的峰，均值会被它们拖走。
    rr_med = float(np.median(rr_c)) if rr_c.size else med
    # 基准也试过用中位数，实测 19.53 S/s + 150 bpm 会出现 +16 bpm 的错误读数。
    cv = 1.4826 * float(np.median(np.abs(rr_c - rr_med))) / (rr_med + 1e-12)
    inl = float(np.mean(np.abs(rr_c - rr_med) <= 0.25 * rr_med))
    # 均值口径：漏搏修正后的区间之和恒等于首尾跨度，所以它等价于"总跨度/修正后段数"，
    # 对采样相位抖动无偏（中位数会量化到整数样本数）。两个估计量互为独立旁证。
    rr_mean = float(rr_c.mean()) if rr_c.size else rr_med
    return {"rr": rr, "rr_c": rr_c, "rr_med": rr_med, "cv": cv, "inl": inl,
            "miss_frac": float(n_miss) / max(int(rr_in.size), 1),
            "rr_base": base,
            "bpm": 60.0 / max(rr_med, 1e-6), "bpm_mean": 60.0 / max(rr_mean, 1e-6)}


# --------------------------------------------------------------------------
# 检测器
# --------------------------------------------------------------------------

class HrDetector:
    """心率检测器：流式 push 或整段 process，只用 numpy。

    用法::  det = HrDetector(fs_measured)   # fs 必须来自 ps_afe.measure_rate()，禁止写死
            res = det.push(code)            # 每个新样本喂一次；ok=False 时显示 "HR --"
    注意：process 与 push 共用同一个环形缓冲，只看缓冲区最后 buffer_sec 秒。
    """

    def __init__(self, fs: float, *, upsample_hz: float = 250.0, buffer_sec: float = 12.0,
                 update_period: float = 0.5, refractory: float = 0.25, min_beats: int = 3,
                 hr_min: float = 30.0, hr_max: float = 220.0, quality_min: float = 0.55,
                 hp_sec: float = 0.2, lp_sec: float = 0.04, integ_sec: float = 0.08,
                 cv_max: float = 0.15, bpm_agree: float = 0.06, min_amp: float = 24.0):
        """参数默认值按 19.53~31.25 S/s 这一档调，换采样率前请重跑 tests/test_ecg_hr.py。

        fs: 实测采样率 (S/s)，唯一强制参数；upsample_hz: 上采样目标 (>= 200 Hz)；
        buffer_sec: 环形缓冲长度；update_period: 流式最小重算间隔；refractory: 不应期 (s，
        生理下限 0.25)；cv_max / bpm_agree: RR 离散度与两种 bpm 口径允许分歧的门槛。
        """
        fs = float(fs)
        if not (fs > 0.0) or not math.isfinite(fs):
            raise ValueError("fs 必须为正的有限值，收到 %r" % (fs,))
        self.fs = fs
        self.upsample_hz = float(upsample_hz)
        self.buffer_sec = float(buffer_sec)
        self.update_period = float(update_period)
        self.refractory = float(refractory)
        self.min_beats = int(min_beats)
        self.hr_min = float(hr_min)
        self.hr_max = float(hr_max)
        self.quality_min = float(quality_min)
        self.hp_sec = float(hp_sec)
        self.lp_sec = float(lp_sec)
        self.integ_sec = float(integ_sec)
        self.cv_max = float(cv_max)
        self.bpm_agree = float(bpm_agree)
        self.min_amp = float(min_amp)

        self._rebuild_fs_dims()            # _cap/_min_samples/_step（依赖 fs，见同名方法）

        self._hist = []                    # 环形缓冲（list + 切片截断，不依赖 deque）
        self._total = 0
        self._since = 0
        self._buf_offset = 0
        # 改动 b：process 真节流用的时间戳。None = 下次调用必须真重算（reset 后即为此）。
        self._last_proc_t = None

        self._result = HrResult(None, 0, False, 0.0, "尚未采集")
        self.last_peaks = np.empty(0, dtype=np.int64)   # 相对缓冲起点的原始样本号
        self.last_offset = 0                            # 缓冲起点在整段里的绝对样本号
        self.last_info = {}                             # 诊断量（离线调参与报告用）

    # ---- 对外 API ----

    @property
    def result(self) -> HrResult:
        """最近一次评估结果（不会返回 None，未采集时 ok=False）。"""
        return self._result

    def _rebuild_fs_dims(self) -> None:
        """按当前 self.fs 重建所有"秒 → 样本"的内部量（__init__ / set_fs 共用）。

        缓冲长度、最小样本数、节流步长都依赖 fs，只改 self.fs 而不重建会继续按旧时基滑。
        """
        fs = float(self.fs)
        self._cap = max(8, int(round(self.buffer_sec * fs)))
        # 2.6 s 是最短可用缓冲：至少要容纳 3 个 150 bpm 的搏（0.4 s 间隔）再加裕量
        self._min_samples = max(8, int(round(2.6 * fs)))
        self._step = max(1, int(round(self.update_period * fs)))

    def set_fs(self, new_fs: float) -> float:
        """换时基（在线重标定入口）：更新 fs、重建尺度、清空检测窗，返回生效值。

        显示/插件负载建立前后 PS 投递率能从 ~31 掉到 ~21 S/s，时间标尺必须跟着换；
        换完必须清空缓冲 —— 混着两个时基算出的 RR 没有物理意义。
        """
        fs = float(new_fs)
        if not (fs > 0.0) or not math.isfinite(fs):
            raise ValueError("fs 必须为正的有限值，收到 %r" % (new_fs,))
        if abs(fs - float(self.fs)) < 1e-12:      # 没变就别清缓冲（白丢 8 s 数据）
            return self.fs
        self.fs = fs
        self._rebuild_fs_dims()
        self.reset()                              # 混合时基的数据必须丢弃，重新积累
        return self.fs

    def reset(self) -> None:
        """清空缓冲与状态；结果回到"尚未采集"。"""
        self._hist = []
        self._total = 0
        self._since = 0
        self._buf_offset = 0
        self._last_proc_t = None        # 清节流时间戳：reset 后首次 process 必须真重算
        self._result = HrResult(None, 0, False, 0.0, "尚未采集")
        self.last_peaks = np.empty(0, dtype=np.int64)
        self.last_offset = 0
        self.last_info = {}

    def push(self, code) -> HrResult:
        """流式喂一个样本（uint16 码值或等价浮点），返回当前结果。

        为了省 CPU，不是每个样本都重算，而是每 update_period 秒重算一次；
        两次重算之间返回上一次的结果，所以调用方可以每个样本都无脑取返回值。
        """
        try:
            v = float(code)
        except (TypeError, ValueError):
            return self._result
        if not math.isfinite(v):
            return self._result
        self._hist.append(v)
        if len(self._hist) > self._cap:
            self._hist = self._hist[-self._cap:]
        self._total += 1
        self._since += 1
        if len(self._hist) >= self._min_samples and (self._since >= self._step or not self.last_info):
            self._recompute()
            self._since = 0
        return self._result

    def process(self, buf, append: bool = False) -> HrResult:
        """批处理整段信号。

        append=False（默认）：先 reset 再把 buf 当作完整一段；append=True：追加进现有缓冲。
        按"距上次真实重算的时长折算成新增样本数"节流：采集主循环每 0.05 s 就重送整窗，
        无条件全量重算会挤占采集线程；不足 update_period 的量直接返回缓存的旧结果。
        """
        arr = np.asarray(buf, dtype=np.float64).ravel()
        if arr.size == 0:
            return self._result
        # 采集主循环每次送的是"最近整窗"，窗口本身不体现增量，故用时间折算新增样本数。
        if (self._last_proc_t is not None
                and (time.monotonic() - self._last_proc_t) * self.fs
                < self.update_period * self.fs):
            return self._result                # 新增样本不足 0.5 s 的量 → 沿用上次结果
        if not append:
            self.reset()
        self._hist.extend(arr.tolist())
        if len(self._hist) > self._cap:
            self._hist = self._hist[-self._cap:]
        self._total += int(arr.size)
        self._since = 0
        self._recompute()
        self._last_proc_t = time.monotonic()   # 记下本次真实重算时刻（节流基准）
        return self._result

    # ---- 内部 ----

    def _fail(self, reason: str, info: dict) -> HrResult:
        nb = int(info.get("n_beats", 0) or 0)
        info = dict(info)
        info.setdefault("ok", False)
        info.setdefault("reason", reason)
        self.last_info = info
        self._result = HrResult(None, nb, False, 0.0, reason)
        self.last_peaks = np.empty(0, dtype=np.int64)
        return self._result

    def _recompute(self) -> HrResult:
        buf = np.asarray(self._hist, dtype=np.float64)
        self._buf_offset = int(self._total - buf.size)
        self.last_offset = self._buf_offset
        return self._detect(buf)

    def _detect(self, x: np.ndarray) -> HrResult:
        info = {"fs": self.fs, "n": int(x.size)}
        n = int(x.size)
        if n < self._min_samples:
            return self._fail("样本不足(%d<%d)" % (n, self._min_samples), info)

        x = np.asarray(x, dtype=np.float64).ravel()
        if not np.all(np.isfinite(x)):
            x = np.where(np.isfinite(x), x, CODE_MID)
        x = x - CODE_MID

        # 饱和比例：真实前端"导联脱落"就是贴地板/天花板。按"距轨 ≤5% 满量程"判（贴轨在
        # code≈125/63061，距满量程仅 ~0.2%，±2 LSB 口径判不出来）。
        rail_band = 0.05 * 2.0 * CODE_MID
        sat_frac = float(np.mean((x <= -CODE_MID + rail_band) | (x >= CODE_MID - rail_band)))
        info["sat_frac"] = sat_frac

        p_lo, p_hi = (float(v) for v in np.percentile(x, [0.5, 99.5]))
        if (p_hi - p_lo) < self.min_amp:
            return self._fail("信号平坦（p0.5~p99.5 仅 %.1f code）" % (p_hi - p_lo), info)

        # 1) 先在原采样率上粗去基线（0.7 s 移动平均 ≈ 1.4 Hz 高通），再上采样。
        #    顺序很重要：先插值会把缓慢漂移"抹"进高频段，反而更难去掉。
        hp0 = x - _movavg_same(x, int(round(0.7 * self.fs)))
        y, fs_up = _upsample(hp0, self.fs, self.upsample_hz)
        y = _bandpass(y, fs_up, self.hp_sec, self.lp_sec)
        info["fs_up"] = fs_up

        # 2) 差分能量包络：乘 fs_up 把"每样本差分"换算成真正的斜率（code/s），
        #    这样阈值口径不随上采样倍数变化。
        d = np.diff(y) * fs_up
        env = _movavg_same(d * d, int(round(self.integ_sec * fs_up)))
        if env.size < 8:
            return self._fail("缓冲过短", info)

        # 3) 候选峰 → 自相关给出若干周期候选 → 占空比/周期性粗筛（见模块头注释第 2 条）
        r = max(1, int(round(0.04 * fs_up)))
        cand = _local_maxima(env, r)
        if cand.size == 0:
            return self._fail("未找到局部极大", info)
        amps = env[cand]
        noise_floor = float(np.median(env))
        # ref 用包络 99 分位：真心电里 QRS 只占很少的样点，99 分位就落在 R 峰附近，
        # 于是阈值会自动跟着 R 波幅值走（换人、换导联位置都不用重调）。
        ref = float(np.percentile(env, 99.0))
        snr = ref / (noise_floor + 1e-9)
        # 幅度对比度用**低分位**基线：心率高时包络几乎是稠密的（150 bpm/19.53 S/s
        # 实测占空比约 0.5），拿中位数当噪声地板会把好信号算成"信噪比只有 3"。
        base_low = float(np.percentile(env, 10.0))
        snr_amp = ref / (base_low + 1e-9)
        # 占空比：真心电的 QRS 是稀疏大峰，包络只有一小段时间是高的；
        # 单频干扰（例如 19.53 S/s 下混叠到 8.6 Hz 的 50 Hz 工频）的包络几乎恒定。
        duty = float(np.mean(env > 0.3 * ref))
        t_min = max(0.25, 60.0 / self.hr_max)
        t_max = min(2.5, 60.0 / self.hr_min)
        periods = _acf_periods(env, fs_up, t_min, t_max)
        info.update(snr=snr, snr_amp=snr_amp, ref=ref, noise_floor=noise_floor, duty=duty,
                    n_cand=int(cand.size), periods=periods)
        if not periods:
            return self._fail("包络无显著周期（无信号或纯噪声）", info)
        if duty > 0.6:
            return self._fail(
                "疑似单频干扰（包络占空比 %.2f，工频混叠进带内时数字陷波救不回来）" % duty, info)

        # 4) 多周期假设 + 逐拍接链，取"能凑出最规整节律"的那一条
        thr_c = max(2.0 * noise_floor, 0.12 * ref)
        keep = cand[amps >= thr_c]
        if keep.size < 3:
            keep = cand[amps >= max(1.5 * noise_floor, 0.05 * ref)]
        if keep.size < 2:
            return self._fail("有效候选峰不足(%d)" % int(keep.size), info)
        low_thr = max(1.5 * noise_floor, 0.03 * ref)

        # 候选周期**只取自相关上显著的那些峰**，不做 T/2、T/3 这类硬性分数档：候选很密时按 T/2 接链
        # 几乎每步都能找到噪声峰，凑出同样规整的 2 倍节律，按搏多者胜就会稳定报出 2 倍心率。
        hyps = sorted(set(round(float(p), 6) for p in periods), reverse=True)
        best = None
        best_score = None
        n_by_T = {}
        for Th in hyps:
            s = _chain_peaks(keep, env[keep], fs_up, Th, low_thr)
            if s.size < 2:
                continue
            s = _apply_refractory(s, env, fs_up, self.refractory)
            if s.size < 2:
                continue
            # 5) R 峰子样本精定位 + 去重（补出来的弱搏可能被吸附到旁边强峰上变成两拍）
            tr = _refine_peaks(y, s, fs_up)
            dm = _dedup_mask(tr, self.refractory)
            if not bool(dm.all()):
                tr = tr[dm]
                s = s[dm]
            if tr.size < 3:
                continue
            st = _rr_stats(tr, self.refractory)
            cvh, inlh, bpmh = st["cv"], st["inl"], st["bpm"]
            n_by_T[round(float(Th), 6)] = int(tr.size)
            # 只有规整节律才有资格当假设，门槛要跟心率走：次谐波串天生更平滑（两个搏的抖动一平均
            # 就小了），固定门槛必然偏袒它。150 bpm/19.53 S/s 时 RR 只有 7~8 个样本，1 个样本的
            # 相位抖动就是 13%，所以门槛下限随 RR 样本数放宽：1.5/RR_samples。
            cv_lim = max(self.cv_max, 1.5 / max(st["rr_med"] * self.fs, 1.0))
            if cvh > cv_lim or inlh < 0.6:
                continue
            if not (self.hr_min <= bpmh <= self.hr_max):
                continue
            # 旁证：中位数口径与均值口径分歧大 = 峰串里混着漏搏/伪峰，谁对谁错说不清，宁可不报。
            if abs(bpmh - st["bpm_mean"]) > self.bpm_agree * max(bpmh, 1.0):
                continue
            score = (int(tr.size), -float(cvh))     # 先比搏数，再比规整度
            if best_score is None or score > best_score:
                best_score, best = score, (s, tr, cvh, Th)
        if best is None:
            return self._fail("无可信节律（各周期假设都凑不出规整 RR）", info)
        sel, t_ref, _, T_used = best
        # 半周期歧义复核（只拒报、绝不改判）：自相关在隔一个搏处往往更强，真周期可能整个漏出候选集
        # （0.41 s 处只有 0.173 < 0.20），于是每两个搏只接一个，报出正好一半的心率。这里用更松
        # 门限再找一次更短周期：若它同样能凑出规整节律，说明一拍还是半拍分不清，直接拒报。
        short_periods = _acf_periods(env, fs_up, t_min,
                                     min(t_max, 0.7 * float(T_used)), min_acf=0.12)
        for Ts in short_periods:
            s3 = _chain_peaks(keep, env[keep], fs_up, float(Ts), low_thr)
            if s3.size < 3:
                continue
            s3 = _apply_refractory(s3, env, fs_up, self.refractory)
            t3 = _refine_peaks(y, s3, fs_up)
            dm3 = _dedup_mask(t3, self.refractory)
            if not bool(dm3.all()):
                t3 = t3[dm3]
            if t3.size <= 1.3 * int(sel.size):
                continue
            st3 = _rr_stats(t3, self.refractory)
            lim3 = max(self.cv_max, 1.5 / max(st3["rr_med"] * self.fs, 1.0))
            if (st3["cv"] <= lim3 and st3["inl"] >= 0.6
                    and self.hr_min <= st3["bpm"] <= self.hr_max
                    and abs(st3["bpm"] - st3["bpm_mean"]) <= self.bpm_agree * st3["bpm"]):
                return self._fail("半周期歧义（更短周期同样能凑出规整节律 %.0f bpm），不做判读"
                                  % st3["bpm"], info)
        # 二义性保护：选定长周期却有更短周期接出明显更多的搏 —— 次谐波，报出去心率只有一半。
        shorter = [n for t, n in n_by_T.items() if t < 0.7 * float(T_used)]
        if shorter and max(shorter) > 1.5 * int(sel.size):
            return self._fail("周期二义性（更短周期接出的搏数明显更多），不做判读", info)
        info["n_keep"] = int(sel.size)
        info["n_beats"] = int(sel.size)
        info["T"] = T_used
        info["n_hyp"] = len(hyps)
        self.last_peaks = np.rint(t_ref * self.fs).astype(np.int64)

        # 6) RR → 漏搏修正 → bpm（规则见 _rr_stats 的 docstring）
        _st = _rr_stats(t_ref, self.refractory)
        rr_med, cv, inl_frac, bpm = _st["rr_med"], _st["cv"], _st["inl"], _st["bpm"]

        # 6b) 只增不改：把拍时刻/RR 组包给 HrResult（周边插件用；既有逻辑不受影响）
        _beats_t = tuple(float(v) for v in t_ref)
        _rr_c = _st.get("rr_c")
        _rr_ms_t = () if _rr_c is None else tuple(float(v) * 1000.0 for v in _rr_c)
        _bidx_t = tuple(int(v) for v in self.last_peaks)

        # 规整度衰减尺度同样跟心率走：150 bpm/19.53 S/s 时 ±1 样本抖动就让 cv 上到 0.15 以上，
        # 那是量化误差不是心律失常，不该按节律崩坏扣分。
        cv_scale = max(0.15, 1.5 / max(rr_med * self.fs, 1.0))
        q_rr = math.exp(-cv / cv_scale) * inl_frac
        q_amp = _clip01(math.log10(max(snr_amp, 1.0)) / math.log10(20.0))
        q_morph = _template_quality(y, sel, fs_up)
        q_duty = 1.0 if duty <= 0.35 else _clip01((0.6 - duty) / 0.25)
        q_cov = _clip01(1.0 - sat_frac / 0.25)
        # 权重按信息量分配（Σ=1.0）：上采样后信号分段线性，模板相关在平直/噪声窗上照样高分，
        # 故 q_morph 降到 0.10，份额给 q_rr 0.35、q_amp 0.30。
        quality = ((q_amp ** 0.30) * (q_rr ** 0.35) * (q_morph ** 0.10)
                   * (q_duty ** 0.15) * (q_cov ** 0.10))
        info.update(bpm=bpm, bpm_mean=_st["bpm_mean"], rr_med=rr_med, cv=cv,
                    inl_frac=inl_frac, miss_frac=_st["miss_frac"], q_rr=q_rr,
                    q_amp=q_amp, q_morph=q_morph, q_duty=q_duty, q_cov=q_cov,
                    quality=quality)

        reasons = []
        if not (self.hr_min <= bpm <= self.hr_max):
            reasons.append("bpm %.0f 超出 %g~%g" % (bpm, self.hr_min, self.hr_max))
        if sel.size < self.min_beats:
            reasons.append("心搏数 %d<%d" % (int(sel.size), self.min_beats))
        if quality < self.quality_min:
            reasons.append("质量 %.2f<%.2f" % (quality, self.quality_min))
        ok = not reasons
        info["ok"] = ok
        reason = "ok" if ok else "；".join(reasons)
        info["reason"] = reason
        self.last_info = info
        self._result = HrResult(round(bpm, 1) if ok else None, int(sel.size),
                                ok, round(quality, 4), reason,
                                beat_idx=_bidx_t, beat_t_s=_beats_t,
                                rr_ms=_rr_ms_t, buf_offset=int(self._buf_offset))
        return self._result
