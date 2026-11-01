"""sim_ecg.py —— 合成心电信号源（只造信号，不碰寄存器；只依赖 numpy）。

在 ~2000 Hz 细栅格成形 → 线性相位抗混叠 → 抽取到 fs，并给出真实 R 峰下标作为真值。
工频在抽取前加入，低采样率下的混叠是真实发生的（50 Hz 折到 ~8.6/12.5 Hz，落在 QRS 带内）。
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["make_ecg", "truth_hr_bpm", "MAINS_HZ"]

CODE_MID = 32768.0        # 与 regs.CODE_MID 一致（此处不 import 是为了让本模块能独立运行）
CODE_MAX = 65535.0
MAINS_HZ = 50.0           # 中国工频

# 单个心搏的形态模板：(相对 R 峰的时间/s, 相对 R 峰幅度的比例, 高斯 sigma/s)
# 参数取成人静息心电的常见值：P 波 ~0.16 s 前，Q/S 夹住 R，T 波 ~0.22 s 后、宽而慢。
_BEAT_COMPONENTS = (
    (-0.160, 0.12, 0.022),        # P
    (-0.028, -0.13, 0.009),       # Q
    (0.000, 1.00, 0.0115),        # R（R 峰就是这个三角/高斯成分的顶点）
    (0.030, -0.26, 0.010),        # S
    (0.220, 0.30, 0.055),         # T
)


def _lpf_ntap(fs: float, cutoff_hz: float, cycles: float = 4.0) -> int:
    """汉宁窗 sinc 的抽头数。

    抽头数与截止频率成反比：过渡带 ~fc/cycles。取 4 个截止周期 ≈ ±25% 过渡带、-40 dB 阻带；
    写死抽头数（如 21）会让低截止频率几乎不起作用 —— 那不是滤波器，是摆设。
    """
    return int(max(5, min(4001, round(cycles * fs / max(float(cutoff_hz), 1e-6))))) | 1


def _lowpass_fir(x: np.ndarray, fs: float, cutoff_hz: float,
                 cycles: float = 4.0) -> np.ndarray:
    """汉宁窗 sinc 低通（严格线性相位），模拟前端抗混叠滤波。

    线性相位保证 R 峰只被整体平移、不被拖变形；np.convolve(mode="same") 对对称核已补偿群时延。
    """
    if cutoff_hz is None or cutoff_hz <= 0.0 or cutoff_hz >= 0.49 * fs:
        return x.copy()
    ntap = _lpf_ntap(fs, cutoff_hz, cycles)
    k = np.arange(ntap, dtype=np.float64) - (ntap - 1) / 2.0
    h = np.sinc(2.0 * cutoff_hz / fs * k) * np.hanning(ntap)
    h_sum = float(h.sum())
    if abs(h_sum) < 1e-12:
        return x.copy()
    return np.convolve(x, h / h_sum, mode="same")


def _hann(n: int) -> np.ndarray:
    if n <= 1:
        return np.ones(max(n, 1), dtype=np.float64)
    return np.hanning(n)


def make_ecg(fs, seconds, hr_bpm=72.0, *, amp=1500.0, noise=10.0, drift=0.0, mains=0.0,
             mains_hz=MAINS_HZ, drift_hz=0.25, artifacts=(), dropouts=(),
             dropout_mode="flat", hrv=0.0, hr_mod=0.0, hr_mod_hz=0.25, aaf_hz=None,
             seed=0, return_truth=False, beat_times=None, beat_amp=None):
    """造一段带标注的合成心电（幅度单位一律是 XADC 码值/LSB：1 V = 65536 码，基线 32768）。

    amp: R 峰幅度(码)；noise/drift/mains: 宽带噪声/基线漂移/工频峰值幅度(码)；
    artifacts/dropouts: [(起始秒, 持续秒[, 强度倍数])]；dropout_mode: "flat" | "rail"；
    aaf_hz: 抗混叠截止(Hz)，None = min(60, 0.45*fs)，0 = 不滤（直接决定低采样率能否用）；
    beat_times/beat_amp: 显式 RR 序列 / 逐拍幅值倍数；return_truth: 另返回真值 R 峰下标。
    """
    fs = float(fs)
    seconds = float(seconds)
    if not (fs > 0.0) or not math.isfinite(fs):
        raise ValueError("fs 必须为正的有限值")
    if not (seconds > 0.0) or not math.isfinite(seconds):
        raise ValueError("seconds 必须为正的有限值")
    if not (hr_bpm > 0.0):
        raise ValueError("hr_bpm 必须为正")

    rng = np.random.default_rng(int(seed))
    n = int(round(seconds * fs))
    if n < 1:
        raise ValueError("seconds*fs 至少要有 1 个样本")

    # 内部细栅格：目标 ~2000 Hz，取 fs 的整数倍，保证 fs 采样点精确落在细栅格上
    os_f = int(max(1, min(200, int(round(2000.0 / fs)))))
    fs_f = fs * os_f
    nf = n * os_f
    tf = np.arange(nf, dtype=np.float64) / fs_f

    # ---- 1) 心搏时刻 ----
    rr0 = 60.0 / float(hr_bpm)
    beats = []
    if beat_times is None:
        t = 0.5 * rr0                      # 从半个 RR 开始，避免 t=0 处出现半截波
        while t < seconds + 0.6 * rr0:
            beats.append(t)
            rr = rr0 * (1.0 + float(hrv) * float(rng.standard_normal())
                        + float(hr_mod) * math.sin(2.0 * math.pi * float(hr_mod_hz) * t))
            t += float(np.clip(rr, 0.25, 3.0))
        beats = np.asarray([b for b in beats if b < seconds + 0.3], dtype=np.float64)
    else:
        # 显式 RR 序列：不做 0.25~3.0 钳制、不筛尾 —— 调用方说了算，否则"停搏 3.4 s"会被钳成 3.0 s。
        beats = np.asarray([float(v) for v in beat_times], dtype=np.float64)

    # ---- 2) 细栅格上叠加每个心搏（只在 ±6 sigma 窗内算，省时间也避免数值噪声）----
    card = np.zeros(nf, dtype=np.float64)
    for k, tb in enumerate(beats):
        # 逐拍幅值倍数（默认 1.0 → 与旧版逐位相同：1.0*x 在 IEEE 下是精确恒等）
        arb = 1.0 if beat_amp is None or k >= len(beat_amp) else float(beat_amp[k])
        for dt, rel, sig in _BEAT_COMPONENTS:
            mu = tb + dt
            half = 6.0 * sig
            i0 = max(0, int((mu - half) * fs_f))
            i1 = min(nf, int((mu + half) * fs_f) + 1)
            if i1 <= i0:
                continue
            seg = tf[i0:i1] - mu
            card[i0:i1] += arb * float(amp) * rel * np.exp(-0.5 * (seg / sig) ** 2)

    # ---- 3) 模拟前端抗混叠（线性相位）----
    # _lowpass_fir 用 np.convolve(mode="same")，对对称核**已经**补偿了群时延（脉冲峰位不变），
    # 所以下面定真值时不能再加 (ntap-1)/2，否则真值整体后移、评分全错。
    aaf = (min(60.0, 0.45 * fs) if aaf_hz is None else float(aaf_hz))
    card = _lowpass_fir(card, fs_f, aaf) if aaf else card.copy()

    # ---- 4) 干扰（漂移 / 工频 / 运动伪迹 / 丢失段）都在抽取前加，保证混叠真实 ----
    y_f = card.copy()
    if drift:
        ph = rng.uniform(0.0, 2.0 * math.pi, size=2)
        y_f += float(drift) * (0.6 * np.sin(2.0 * math.pi * float(drift_hz) * tf + ph[0])
                               + 0.4 * np.sin(2.0 * math.pi * 0.37 * tf + ph[1]))
    if mains:
        y_f += float(mains) * np.sin(2.0 * math.pi * float(mains_hz) * tf
                                     + rng.uniform(0.0, 2.0 * math.pi))
    for spec in artifacts:
        a0, dur = float(spec[0]), float(spec[1])
        strength = float(spec[2]) if len(spec) > 2 else 3.0
        i0 = max(0, int(a0 * fs_f))
        i1 = min(nf, int((a0 + dur) * fs_f))
        if i1 <= i0:
            continue
        env = _hann(i1 - i0)
        tt = tf[i0:i1] - a0
        y_f[i0:i1] += float(amp) * strength * env * (0.7 * np.sin(2.0 * math.pi * 4.0 * tt)
                                                     + 0.5 * rng.standard_normal(i1 - i0))
    lost = np.zeros(nf, dtype=bool)
    for spec in dropouts:
        a0, dur = float(spec[0]), float(spec[1])
        i0 = max(0, int(a0 * fs_f))
        i1 = min(nf, int((a0 + dur) * fs_f))
        if i1 > i0:
            lost[i0:i1] = True
    if lost.any():
        if str(dropout_mode) == "rail":
            y_f[lost] = 0.98 * (CODE_MAX - CODE_MID)
        else:
            y_f[lost] = 0.0

    # ---- 5) 抽取到 fs，再按 ADC 采样点加噪声（噪声不随 fs 变，这是 ADC 的噪声地板）----
    y = y_f[::os_f][:n]
    if noise:
        y = y + float(noise) * rng.standard_normal(y.size)

    raw = np.clip(np.rint(y + CODE_MID), 0.0, CODE_MAX).astype(np.uint16)

    if not return_truth:
        return raw

    # ---- 6) 真值 R 峰：在"干净的心电（已过抗混叠、未加干扰）"上找顶点 ----
    #     这样真值就是**算法理论上能到达的最好位置**，而不是理想脉冲的位置。
    clean = card[::os_f][:n]
    idx = []
    for tb in beats:
        i0 = max(0, int((tb - 0.06) * fs))
        i1 = min(n, int((tb + 0.06) * fs) + 1)
        if i1 <= i0:
            continue
        if lost[::os_f][:n][i0:i1].any():      # 丢失段内的真值剔除，不参与评分
            continue
        # 在原始采样栅格上取"干净信号"的极值点（近似连续顶点的最近样本）
        j = i0 + int(np.argmax(np.abs(clean[i0:i1])))
        idx.append(j)
    return raw, np.asarray(sorted(set(int(v) for v in idx)), dtype=np.int64)


def truth_hr_bpm(r_idx, fs):
    """由真值 R 峰下标反算心率（用中位数 RR），用于自检造出来的信号是不是设定心率。"""
    r = np.asarray(r_idx, dtype=np.float64) / float(fs)
    if r.size < 2:
        return None
    return 60.0 / float(np.median(np.diff(r)))


if __name__ == "__main__":                # 手工冒烟：python -m ecgb.sim_ecg
    for f in (19.53, 31.25):
        for hr in (40.0, 72.0, 150.0):
            sig, ridx = make_ecg(f, 20.0, hr, noise=80.0, drift=800.0, return_truth=True)
            print("fs=%6.2f 设定=%6.1f 真值中位数=%6.1f  长度=%d  峰数=%d"
                  % (f, hr, truth_hr_bpm(ridx, f), sig.size, ridx.size))
