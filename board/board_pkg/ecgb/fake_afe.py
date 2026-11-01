"""fake_afe.py —— 离线 AFE 行为模型（afe_ctrl_0 的纯软件替身，只用 numpy + 标准库）。

复刻 4 个寄存器语义并保持 PYNQ DefaultIP 同形（read(offset, length=4) / write(offset, value)）。
样本按虚拟节拍生成、时间只走注入的 clock，同 seed 逐样本可复现；fs 是标定量，不写死。
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, Optional

import numpy as np

try:  # 包内导入（import ecgb.fake_afe / pytest）
    from . import regs
except ImportError:  # 直接 python src/ecgb/fake_afe.py 时退回平级导入
    import regs  # type: ignore

__all__ = ["FakeClock", "FakeAfe", "make_fake_pynq_overlay"]

# 默认有效采样率：取实测缺口值，而不是理论值 31.25
FS_DEFAULT = float(regs.FS_MEASURED_1005)


class FakeClock:
    """确定性虚拟时钟。

    单独成类而非 lambda：PsAfe 靠"时钟有没有 advance()"判断该推进虚拟时间还是真睡眠。
    """

    def __init__(self, t0: float = 0.0) -> None:
        self._t = float(t0)

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> float:
        if dt < 0:
            raise ValueError(f"虚拟时钟不能倒流：dt={dt}")
        self._t += float(dt)
        return self._t

    def set(self, t: float) -> None:
        """直接跳到某时刻（手搓时钟脚本时用）。"""
        self._t = float(t)

    @property
    def t(self) -> float:
        return self._t

    def __repr__(self) -> str:
        return f"FakeClock(t={self._t:.6f})"


class FakeAfe:
    """afe_ctrl_0 的寄存器级行为模型（幅度类参数单位均为 XADC 码值）。

    fs/rate: 有效采样率 S/s，都缺省时取 19.53；seed: 噪声与抖动共用的随机种子；
    filt_fc: 0x08 滤波链 -3dB 截止频率 (Hz，被 fs 奈奎斯特夹住)；clock: 注入时钟，
    传 FakeClock 即确定性回放；source: 自定义信号源（f(t) 或按 fs 排列的数组），
    给了就由调用方负责整段波形，基线/呼吸/工频不再叠加。
    """

    def __init__(self, rate: Optional[float] = None, seed: int = 0, *,
                 fs: Optional[float] = None, jitter: float = 0.0,
                 noise: float = 25.0, ecg_amp: float = 1200.0,
                 resp_amp: float = 300.0, mains_amp: float = 120.0,
                 hr_hz: float = 1.2, resp_hz: float = 0.3,
                 mains_hz: float = 50.0, baseline: float = 32768.0,
                 filt_fc: float = 40.0,
                 clock: Optional[Callable[[], float]] = None,
                 enable: bool = True, source: Any = None) -> None:
        if fs is None:
            fs = FS_DEFAULT if rate is None else float(rate)
        fs = float(fs)
        if fs <= 0.0:
            raise ValueError(f"fs 必须为正：{fs}")
        if not (0.0 <= jitter < 1.0):
            raise ValueError(f"jitter 需在 [0,1)：{jitter}")

        self.fs = fs
        self.rate = fs          # fs 的别名：调用方用 sim.rate 读"设定采样率"更顺手
        self.fs_theory = float(regs.FS_THEORY)
        self.seed = int(seed)
        self.jitter = float(jitter)
        self.noise = float(noise)
        self.ecg_amp = float(ecg_amp)
        self.resp_amp = float(resp_amp)
        self.mains_amp = float(mains_amp)
        self.hr_hz = float(hr_hz)
        self.resp_hz = float(resp_hz)
        self.mains_hz = float(mains_hz)
        self.baseline = float(baseline)
        self.filt_fc = float(filt_fc)
        self.clock: Callable[[], float] = time.monotonic if clock is None else clock
        self._rng = np.random.default_rng(self.seed)
        self.source = source
        self._source_arr: Optional[np.ndarray] = None
        if source is not None and not callable(source):
            arr = np.asarray(source, dtype=np.float64).ravel()
            if arr.size == 0:
                raise ValueError("source 数组不能为空")
            self._source_arr = arr

        # ---- 0x00 / 0x04 / 0x08 / 0x0C 的寄存器镜像 ----
        self._ctrl = regs.CTRL_EN if enable else 0
        self._cnt = 0
        self._raw = self._clamp_code(self.baseline)
        self._filt = float(self.baseline)
        self._pending_new = False

        # 单极点 IIR：fc 超过奈奎斯特就夹到 0.45*fs —— 真实模拟滤波链也做不到更好
        self.filt_fc_eff = min(self.filt_fc, 0.45 * self.fs)
        self._alpha = 1.0 - math.exp(-2.0 * math.pi * self.filt_fc_eff / self.fs)

        # ---- 虚拟节拍状态 ----
        self._last_sample_t: Optional[float] = None
        self._n_emitted = 0
        now0 = float(self.clock())
        # 构造时就定下第一拍：否则"先 advance 再第一次读"会把这段时间白白吞掉
        self._next_t: Optional[float] = (now0 + self._draw_interval()) if self._ctrl else None

    # ------------------------------------------------------------- 波形与节拍
    @staticmethod
    def _clamp_code(v: float) -> int:
        """XADC 是 16 bit 无符号：越界就削顶/削底（真实硬件饱和也是这个行为）。"""
        if v <= 0.0:
            return 0
        if v >= 65535.0:
            return 65535
        return int(round(v))

    def _ecg_shape(self, t: float) -> float:
        """PQRST 的相对形状（R 峰 = 1.0）。用高斯包叠加，数学上带限、可复现。"""
        period = 1.0 / self.hr_hz
        k = math.floor(t / period)
        u = t - (k + 0.30) * period          # u = 距本搏 R 峰的秒数（可正可负）
        p = math.exp(-0.5 * (u + 0.160) ** 2 / 0.030 ** 2)
        q = math.exp(-0.5 * (u + 0.022) ** 2 / 0.009 ** 2)
        r = math.exp(-0.5 * u ** 2 / 0.012 ** 2)
        s = math.exp(-0.5 * (u - 0.028) ** 2 / 0.012 ** 2)
        tt = math.exp(-0.5 * (u - 0.220) ** 2 / 0.060 ** 2)
        return 0.12 * p - 0.15 * q + 1.00 * r - 0.25 * s + 0.28 * tt

    def _waveform(self, t: float) -> float:
        """某一采样时刻的"AD8232 + XADC"码值（未 clamp）。"""
        v = self.baseline
        v += self.ecg_amp * self._ecg_shape(t)
        v += self.resp_amp * math.sin(2.0 * math.pi * self.resp_hz * t)
        # 50 Hz 工频是被"采"进去的正弦：fs 不够高时自然混叠 —— 这是真实物理后果，
        # 也说明"50 Hz 陷波在 19.53 S/s 下救不了你"（采样前就已经折叠了）。
        v += self.mains_amp * math.sin(2.0 * math.pi * self.mains_hz * t)
        v += self.noise * float(self._rng.standard_normal())
        return v

    def _draw_interval(self) -> float:
        """抽下一拍的间隔。jitter=0 时不消耗 RNG，保证零抖动路径算术上完全确定。"""
        base = 1.0 / self.fs
        if self.jitter <= 0.0:
            return base
        return base * (1.0 + self.jitter * float(self._rng.uniform(-1.0, 1.0)))

    def _update_filt(self, code: int) -> float:
        """0x08 的口径：滤波链没使能或强制旁路 → 直通原始样本；否则走单极点低通。"""
        if (self._ctrl & regs.CTRL_FILT_EN) == 0 or (self._ctrl & regs.CTRL_BYPASS):
            return float(code)
        return self._filt + self._alpha * (float(code) - self._filt)

    def _source_value(self, t: float, n: int) -> float:
        """取第 n 拍的波形值：给了 source 就完全交给它，否则走内置模型。"""
        if self.source is None:
            return self._waveform(t)
        if callable(self.source):
            return float(self.source(t))
        assert self._source_arr is not None
        return float(self._source_arr[min(max(n, 0), self._source_arr.size - 1)])

    def _emit(self, t: float) -> None:
        self._cnt = (self._cnt + 1) & 0xFFFFFFFF
        code = self._clamp_code(self._source_value(t, self._n_emitted))
        self._raw = code
        self._filt = self._update_filt(code)
        self._pending_new = True
        self._last_sample_t = t
        self._n_emitted += 1

    def _tick(self, now: float) -> None:
        """把虚拟时间推进到 now：补齐这段时间内本应发生的所有采样节拍。"""
        if (self._ctrl & regs.CTRL_EN) == 0:
            return
        if self._next_t is None:
            self._next_t = now + self._draw_interval()
        while self._next_t <= now:
            self._emit(self._next_t)
            self._next_t += self._draw_interval()

    # ------------------------------------------------------- PYNQ 同形寄存器口
    def read(self, offset: int, length: int = 4) -> int:
        """与 PYNQ DefaultIP/MMIO 同形：read(offset, length=4) -> int。

        每次读先把虚拟时间推进到"现在"：采样由时间驱动而非读操作驱动，读几次都不改变样本序列。
        """
        off = int(offset) & 0xFF
        self._tick(float(self.clock()))
        if off == regs.REG_CTRL:
            return int(self._ctrl) & 0xFFFFFFFF
        if off == regs.REG_CNT:
            return int(self._cnt) & 0xFFFFFFFF
        if off == regs.REG_FILT:
            # 真实 IP 的 [31] filt_valid 是单周期脉冲（约 32 ns @ 31.25 MHz），轮询读不到 → 恒 0
            return int(round(self._filt)) & 0xFFFF
        if off == regs.REG_RAW:
            flag = 1 if self._pending_new else 0
            self._pending_new = False        # 「读清」语义的落点：这一行只执行一次
            return (int(self._raw) & 0xFFFF) | (flag << 31)
        raise KeyError(f"FakeAfe 不认识的寄存器偏移：0x{off:02X}")

    def write(self, offset: int, value: int) -> None:
        """只对 0x00 有效；RO 寄存器写入按真实 IP 的行为静默忽略（不抛异常）。"""
        off = int(offset) & 0xFF
        now = float(self.clock())
        # 先结算到"现在"再改控制位：否则 en 从 1 翻到 0 时，中间跨过的时间会被丢掉
        self._tick(now)
        if off != regs.REG_CTRL:
            return
        was_en = bool(self._ctrl & regs.CTRL_EN)
        self._ctrl = int(value) & 0x0F
        now_en = bool(self._ctrl & regs.CTRL_EN)
        if now_en and not was_en:
            # 重新使能：真实硬件里 tick_gen 计数被清零，不补发历史样本 → 节拍从此刻重算
            self._next_t = now + self._draw_interval()
        elif not now_en:
            self._next_t = None

    def close(self) -> None:
        """与 PYNQ IP 对象同形的收尾口；本模型无资源可释放。"""
        return None

    def snapshot(self) -> Dict[str, Any]:
        """给测试/调试看的寄存器与内部状态快照（不改变任何状态）。"""
        return {
            "fs": self.fs,
            "ctrl": self._ctrl,
            "sample_cnt": self._cnt,
            "raw": int(self._raw),
            "filt": int(round(self._filt)),
            "pending_new": bool(self._pending_new),
            "last_sample_t": self._last_sample_t,
            "n_emitted": self._n_emitted,
        }

    def __repr__(self) -> str:
        return (f"FakeAfe(fs={self.fs:g} S/s, seed={self.seed}, ctrl=0x{self._ctrl:02X}, "
                f"cnt={self._cnt}, raw={int(self._raw)}, filt={int(round(self._filt))})")


class _FakeOverlay:
    """最小 PYNQ Overlay 替身：属性访问 afe_ctrl_0 就是 IP，用于没板子的机器。"""

    def __init__(self, afe: FakeAfe, ip_name: str = regs.IP_NAME_DEFAULT) -> None:
        # 契约要求无论如何都有 .afe_ctrl_0；ip_name 不同时再多挂一个别名
        setattr(self, "afe_ctrl_0", afe)
        if ip_name != "afe_ctrl_0":
            setattr(self, ip_name, afe)
        self.ip_dict = {ip_name: {"base_address": regs.IP_BASE_FALLBACK,
                                  "addr_range": regs.IP_RANGE_FALLBACK}}
        self.bitfile_name = "<fake_afe 离线替身>"

    def __repr__(self) -> str:
        return f"_FakeOverlay({self.afe_ctrl_0!r})"


def make_fake_pynq_overlay(rate: Optional[float] = None, seed: int = 0,
                           ip_name: str = regs.IP_NAME_DEFAULT, **afe_kw: Any) -> _FakeOverlay:
    """造一个"假 Overlay"：ol.afe_ctrl_0 即 FakeAfe，可直接喂给 PsAfe(sim=...)。

    也允许 afe=<现成的 FakeAfe> 复用同一个模型实例（例如要让驱动与模型共享时钟）。
    """
    afe = afe_kw.pop("afe", None)
    if afe is None:
        afe = FakeAfe(rate=rate, seed=seed, **afe_kw)
    return _FakeOverlay(afe, ip_name=ip_name)
