"""ps_afe.py —— 板级 AFE 驱动（afe_ctrl_0），无板时自动降级到离线 sim 后端。

后端优先级：pynq.Overlay 属性访问 → pynq.MMIO(0x43C00000) → FakeAfe（打横幅，sim 数据不可
当作上板结论）。capture() 只认 0x0C[31] new_flag 且每拍只读一次（读清）；fs 口径 = cnt 增量/耗时。
"""

from __future__ import annotations

import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

try:
    from . import regs
except ImportError:  # 直接 python src/ecgb/ps_afe.py 时退回平级导入
    import regs  # type: ignore

__all__ = ["PsAfe"]

_SIM_BANNER = (
    "\n" + "!" * 68 + "\n"
    "[PsAfe] 未检测到可用的 PYNQ / MMIO 后端，已自动降级到 **离线 sim 后端**。\n"
    "        数据来自 fake_afe.FakeAfe（纯软件模型），不是真实硬件，\n"
    "        不可用于上板结论；若此时人就在 PYNQ-Z2 板子上，请先排查\n"
    "        pynq 导入 / Overlay 下载 / bit 路径，不要拿 sim 数据交差。\n"
    + "!" * 68 + "\n"
)


class PsAfe:
    """afe_ctrl_0 的板级驱动 / 离线后端适配器。

    bit_path: .bit（或 .hwh）路径，None 用 PYNQ 默认 Overlay；ip_name: IP 实例名（默认 afe_ctrl_0）。
    sim: 离线替身（带 read/write 的 IP 对象，或带 IP 属性的 Overlay 替身）；显式传入时不再触碰 pynq。
    clock: 注入时钟（秒）；传带 advance() 的 FakeClock 即进入虚拟时间模式，capture/measure_rate
    改为推进虚拟时间，离线回放既快又可逐位复现。
    """

    def __init__(self, bit_path: Optional[str] = None, sim: Any = None,
                 ip_name: str = regs.IP_NAME_DEFAULT, verbose: bool = True,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self.bit_path = bit_path
        self.ip_name = str(ip_name)
        self.verbose = bool(verbose)
        self.backend: str = "none"
        self.ip: Any = None
        self._ol: Any = None
        self._errors: List[str] = []
        self._clock = clock
        self._last_ctrl: Optional[int] = None   # 0x00 的写入镜像：set_band 要保留其它位

        if sim is not None:
            self._bind_sim(sim, explicit=True)
        elif self._try_pynq(bit_path):
            pass
        elif self._try_mmio():
            pass
        else:
            self._bind_sim(self._auto_sim(), explicit=False)

        # 时钟优先级：用户注入 > sim 模型自带（让驱动与模型共享同一虚拟时间）> 墙上时钟
        if self._clock is None:
            self._clock = getattr(self.ip, "clock", None) or time.monotonic
        # 有没有 advance() 决定"推进虚拟时间"还是"真睡眠"（鸭子类型，见类 docstring）
        self._virtual = hasattr(self._clock, "advance")
        self._log(f"[PsAfe] 后端={self.backend} ip={self.ip_name} "
                  f"clock={'virtual' if self._virtual else 'wall'}")

    # --------------------------------------------------------------- 后端选择
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr)

    def _try_pynq(self, bit_path: Optional[str]) -> bool:
        try:
            from pynq import Overlay  # type: ignore
        except Exception as exc:  # ImportError 最常见，但 pynq 初始化异常也要兜住
            self._errors.append(f"pynq.Overlay 不可用: {exc.__class__.__name__}: {exc}")
            return False
        try:
            ol = Overlay(bit_path) if bit_path else Overlay()
            ip = getattr(ol, self.ip_name)
        except Exception as exc:
            self._errors.append(f"Overlay 加载/属性访问失败: {exc.__class__.__name__}: {exc}")
            return False
        self._ol, self.ip, self.backend = ol, ip, "pynq"
        self._log(f"[PsAfe] 使用 PYNQ Overlay 后端，IP={self.ip_name}")
        return True

    def _try_mmio(self) -> bool:
        try:
            from pynq import MMIO  # type: ignore
        except Exception as exc:
            self._errors.append(f"pynq.MMIO 不可用: {exc.__class__.__name__}: {exc}")
            return False
        try:
            ip = MMIO(regs.IP_BASE_FALLBACK, regs.IP_RANGE_FALLBACK)
        except Exception as exc:
            self._errors.append(
                f"MMIO 映射 0x{regs.IP_BASE_FALLBACK:08X} 失败: {exc.__class__.__name__}: {exc}")
            return False
        self.ip, self.backend = ip, "mmio"
        self._log(f"[PsAfe] 使用 MMIO 后端 0x{regs.IP_BASE_FALLBACK:08X} "
                  f"（range=0x{regs.IP_RANGE_FALLBACK:X}）")
        return True

    def _auto_sim(self) -> Any:
        try:
            try:
                from .fake_afe import make_fake_pynq_overlay
            except ImportError:
                from fake_afe import make_fake_pynq_overlay  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "PsAfe 三级后端全部失败："
                + "；".join(self._errors)
                + f"；且无法导入离线模拟器 fake_afe（{exc}）"
            ) from exc
        # 时钟交给 FakeAfe 决定（clock=None → 它自己用 time.monotonic）
        return make_fake_pynq_overlay(clock=self._clock)

    def _bind_sim(self, sim: Any, explicit: bool) -> None:
        ip = getattr(sim, self.ip_name, None)
        if ip is None:
            ip = getattr(sim, "afe_ctrl_0", None)
        if ip is None and hasattr(sim, "read") and hasattr(sim, "write"):
            ip = sim  # 直接塞一个 IP 对象也允许
        if ip is None or not (hasattr(ip, "read") and hasattr(ip, "write")):
            raise TypeError(
                f"sim 必须是含 read/write 的 IP 对象，或带 {self.ip_name} 属性的 Overlay 替身；"
                f"收到 {type(sim).__name__}")
        self.ip, self.backend = ip, "sim"
        if not explicit:
            print(_SIM_BANNER, file=sys.stderr)
        self._log(f"[PsAfe] 使用 sim 后端（{'显式注入' if explicit else '自动降级'}）")

    # --------------------------------------------------------------- 寄存器读写
    def _rd(self, offset: int) -> int:
        """读 32 bit 寄存器。PYNQ 的 DefaultIP/MMIO 都是 read(offset, length)。"""
        try:
            return int(self.ip.read(offset, 4))
        except TypeError:
            return int(self.ip.read(offset))  # 某些替身只实现了 read(offset)

    def _wr(self, offset: int, value: int) -> None:
        self.ip.write(offset, int(value) & 0xFFFFFFFF)

    # ------------------------------------------------------------------ 时间
    def _now(self) -> float:
        return float(self._clock())

    def _sleep(self, dt: float) -> None:
        """统一"等待"入口：虚拟时钟 → 推进时间；墙上时钟 → 真睡眠。"""
        if dt <= 0.0:
            return
        if self._virtual:
            self._clock.advance(dt)
        else:
            time.sleep(dt)

    # ------------------------------------------------------------------ 控制
    def start(self, filt_en: bool = False, bypass: bool = False,
              band: Optional[int] = None) -> int:
        """使能采样：0x00 = EN | filt_en | bypass | (band << 2)。返回写入值。

        band 是接口表 Sheet④ BA-04 的 `[3:2]` 带宽档位（0/1/2/3 = 40/25/15/保留 Hz），
        **当前 RTL 未实现，写了不生效**；传它只是把位拼对，等 A 实现后立刻可用。
        """
        prev = self._last_ctrl or 0
        keep_band = band is None
        ctrl = regs.ctrl_word(en=True, filt_en=filt_en, bypass=bypass,
                              band=prev >> regs.BAND_LSB if keep_band else band)
        self._wr(regs.REG_CTRL, ctrl)
        self._last_ctrl = ctrl
        self._log(f"[PsAfe] start(filt_en={filt_en}, bypass={bypass}, band={band}) "
                  f"-> ctrl=0x{ctrl:02X}")
        return ctrl

    def set_band(self, band: int) -> int:
        """设置 0x00[3:2] 带宽档位（接口表 BA-04），保留 en/filt_en/bypass 不变。返回写入值。

        ⚠️ 当前 RTL 未实现该字段，写入**不会**改变滤波带宽；本方法只是把 B 侧口径
        先对齐表，等 A 实现后无需改 B 代码。
        """
        b = int(band)
        if not 0 <= b <= 3:
            raise ValueError(f"带宽档位只能是 0..3（40/25/15/保留 Hz），收到 {band}")
        cur = self._last_ctrl if self._last_ctrl is not None else regs.CTRL_EN
        ctrl = (cur & ~regs.BAND_MASK) | ((b << regs.BAND_LSB) & regs.BAND_MASK)
        self._wr(regs.REG_CTRL, ctrl)
        self._last_ctrl = ctrl
        self._log(f"[PsAfe] set_band({b}) -> ctrl=0x{ctrl:02X}")
        return ctrl

    @property
    def band(self) -> int:
        """当前档位（来自本驱动最近一次写入的镜像；RTL 未实现时读回无意义）。"""
        return int((self._last_ctrl or 0) >> regs.BAND_LSB) & 0b11

    def stop(self) -> None:
        """清 0x00：en=0 后 PL 的 tick_gen 停走，sample_cnt 冻结但**不清零**。"""
        self._wr(regs.REG_CTRL, 0)

    def reset_ctrl(self) -> None:
        """只清控制位（写 0x00=0，即停采样 + 清滤波/旁路/档位位）。

        0x00 里**没有**可用的软复位位（[3:2] 是带宽档位、[5..31] 为预留）；要复位 PL 请重载 overlay。
        """
        self._wr(regs.REG_CTRL, 0)

    # ------------------------------------------------------------- 只读寄存器
    def read_cnt(self) -> int:
        """0x04 sample_cnt（32 bit，只增）。"""
        return self._rd(regs.REG_CNT) & 0xFFFFFFFF

    def read_filt(self) -> int:
        """0x08[15:0] 滤波输出。注意 [31] filt_valid 轮询读不到，返回里没有它。"""
        return self._rd(regs.REG_FILT) & 0xFFFF

    def read_raw(self) -> Tuple[int, bool]:
        """读 0x0C **一次**，返回 (sample_raw, new_flag)。读清语义在这里落地。"""
        word = self._rd(regs.REG_RAW)
        return word & 0xFFFF, bool(word & 0x80000000)

    # ------------------------------------------------------------------ 测速
    def measure_rate(self, window: float = 2.0) -> float:
        """用 sample_cnt 增量 / 实际耗时测有效采样率 S/s —— 全项目 fs 的权威口径。

        不用"样本数/时长"：慢轮询会漏样本，算出来偏低；cnt 由 PL 计数，与 PS 轮询快慢无关。
        窗口内周期性看一眼 cnt（不是死等），避免把一次极长读拖成不可控时延。
        """
        if window <= 0:
            raise ValueError(f"window 必须为正：{window}")
        t0 = self._now()
        c0 = self.read_cnt()
        dt_poll = min(0.02, max(window / 10.0, 1e-6))
        t1, c1 = t0, c0
        while t1 - t0 < window:
            self._sleep(dt_poll)
            c1 = self.read_cnt()
            t1 = self._now()
        elapsed = max(t1 - t0, 1e-12)
        delta = (c1 - c0) & 0xFFFFFFFF  # cnt 只增；万一 32 bit 回转，掩码后仍非负
        fs = delta / elapsed
        self._log(f"[PsAfe] measure_rate({window:g}s): d_cnt={delta}, "
                  f"dt={elapsed:.4f}s -> fs={fs:.4f} S/s")
        return float(fs)

    # ------------------------------------------------------------------ 采集
    def capture(self, seconds: float, use_filt: bool = True, poll_dt: float = 0.0,
                rate: Optional[float] = None
                ) -> Tuple[np.ndarray, float, Dict[str, Any]]:
        """连续采集 seconds 秒，返回 (wave, fs_measured, stats)。

        use_filt: True 取 0x08 滤波值，False 取 0x0C 原始样本；两种都只靠 0x0C[31] 判新样本。
        poll_dt: >0 固定间隔；0 = 能多快就多快；None = 自动（约 1/4 采样周期）。
        stats 关键键：n_samples / duration / fs_measured（cnt 口径，权威）/ fs_from_samples /
        dup_reads / missed_samples；判断节拍是否正常请用 est_interval_median（min 含半截间隔）。
        """
        if seconds <= 0:
            raise ValueError(f"seconds 必须为正：{seconds}")
        fs_hint = float(rate) if rate else None
        if poll_dt is None:
            # 轮询比采样快约 4 倍：既有分辨率，又不至于把 CPU 打满
            base = fs_hint if fs_hint else self.measure_rate(
                window=min(1.0, max(seconds / 4.0, 0.1)))
            poll_dt = 1.0 / max(base, 1e-3) / 4.0
        poll_dt = float(poll_dt)
        if poll_dt <= 0.0:
            if self._virtual:
                # 虚拟时钟必须自己"走"：取 1/64 采样周期，等价于"很忙的轮询"
                poll_dt = 1.0 / (fs_hint or float(regs.FS_MEASURED_1005)) / 64.0
            else:
                poll_dt = 0.0  # 真·忙轮询，不睡眠

        t0 = self._now()
        c0 = self.read_cnt()
        samples: List[int] = []
        stamps: List[float] = []
        polls = 0
        dups = 0
        stall = 0
        last_now = t0

        while True:
            now = self._now()
            # 用 > 而不是 >=：让恰好落在窗口末尾的那一拍也被轮询到。否则当 fs 与窗口长度
            # 成整数比（例如 31.25 S/s × 8 s = 250 拍）时，边界样本会稳定地漏掉 1 个。
            if now - t0 > seconds:
                break
            if now <= last_now:
                # 时钟不前进（注入的假时钟忘了 advance）：再转下去就是死循环，早报错
                stall += 1
                if stall > 200000:
                    raise RuntimeError(
                        "capture 检测到时钟未前进：注入的 clock 需要提供 advance(dt)")
            else:
                stall = 0
                last_now = now

            word = self._rd(regs.REG_RAW)  # 唯一的采样判定动作：一次读算一次
            polls += 1
            if word & 0x80000000:
                samples.append(self.read_filt() if use_filt else (word & 0xFFFF))
                stamps.append(now)
            else:
                dups += 1
            self._sleep(poll_dt)

        t1 = self._now()
        c1 = self.read_cnt()
        duration = max(t1 - t0, 1e-12)
        cnt_delta = (c1 - c0) & 0xFFFFFFFF
        fs_measured = cnt_delta / duration            # 权威口径：与轮询快慢无关
        fs_from_samples = len(samples) / duration     # 交叉检查：慢轮询时会明显偏低
        ivs = (np.diff(np.asarray(stamps, dtype=np.float64))
               if len(stamps) > 1 else np.empty(0, dtype=np.float64))

        stats: Dict[str, Any] = {
            "n_samples": len(samples),
            "duration": float(duration),
            "fs_measured": float(fs_measured),
            "dup_reads": int(dups),
            "new_flag_false_ratio": float(dups / polls) if polls else 0.0,
            "est_interval_min": float(ivs.min()) if ivs.size else float("nan"),
            "est_interval_max": float(ivs.max()) if ivs.size else float("nan"),
            "n_polls": int(polls),
            "fs_from_samples": float(fs_from_samples),
            "cnt_delta": int(cnt_delta),
            "est_interval_mean": float(ivs.mean()) if ivs.size else float("nan"),
            # 中位数不受"窗口起点半截间隔"和边界效应影响，判断节拍是否正常看它
            "est_interval_median": float(np.median(ivs)) if ivs.size else float("nan"),
            "missed_samples": int(max(0, cnt_delta - len(samples))),
            "use_filt": bool(use_filt),
            "poll_dt": float(poll_dt),
            "backend": self.backend,
        }
        wave = np.asarray(samples, dtype=np.uint16)
        if cnt_delta == 0:
            self._log("[PsAfe] 警告：窗口内 sample_cnt 无增量 —— 确认已 start()"
                      "（0x00[0]=1）且 PL 在跑")
        if fs_hint is not None and fs_hint > 0 and cnt_delta > 0:
            dev = abs(fs_measured - fs_hint) / fs_hint
            if dev > 0.2:
                self._log(f"[PsAfe] 注意：实测 fs={fs_measured:.3f} 与预期 {fs_hint:g} "
                          f"相差 {dev * 100:.1f}%（fs 是标定量，以实测为准）")
        self._log(f"[PsAfe] capture({seconds:g}s): n={stats['n_samples']}, "
                  f"fs={fs_measured:.4f} S/s（cnt 口径）, dup={dups}/{polls}, "
                  f"missed={stats['missed_samples']}")
        return wave, float(fs_measured), stats


def _force_utf8_when_piped() -> None:
    """管道/重定向时把 stdout 切到 UTF-8（Windows 默认 GBK 会让日志端中文全乱码）。

    真实控制台（isatty）保持系统编码不动；只在自查入口调用。
    """
    try:
        if not sys.stdout.isatty() and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _selfcheck(seconds: float = 30.0) -> int:
    """无板自查：FakeAfe + 假时钟跑 seconds 秒虚拟采集，打印样本数/fs/dup 比例。

    用假时钟而不是真睡：这条自查要能进 CI，必须秒级完成且可复现。
    """
    _force_utf8_when_piped()
    try:
        from .fake_afe import FakeClock, make_fake_pynq_overlay
    except ImportError:
        from fake_afe import FakeClock, make_fake_pynq_overlay  # type: ignore

    fs_hint = float(regs.FS_MEASURED_1005)
    clk = FakeClock(0.0)
    overlay = make_fake_pynq_overlay(rate=fs_hint, seed=7, clock=clk, jitter=0.0)
    drv = PsAfe(sim=overlay, verbose=False, clock=clk)
    assert drv.backend == "sim", drv.backend
    drv.start(filt_en=True)

    rate = drv.measure_rate(window=2.0)
    wave, fs_meas, st = drv.capture(seconds=seconds, use_filt=True,
                                    poll_dt=0.0, rate=fs_hint)
    dup_ratio = (st["dup_reads"] / st["n_polls"]) if st["n_polls"] else 0.0
    ok = abs(fs_meas - fs_hint) / fs_hint <= 0.05 and st["n_samples"] > 0

    print("=" * 72)
    print("PsAfe 无板自查（FakeAfe + FakeClock，纯虚拟时间，可复现）")
    print("=" * 72)
    print(f"后端                : sim / FakeAfe(fs={fs_hint} S/s, seed=7)")
    print(f"measure_rate(2.0s)  : {rate:.4f} S/s  （sample_cnt 增量 / 实际耗时，权威口径）")
    print(f"capture({seconds:g}s)       : n_samples={st['n_samples']}, dtype={wave.dtype}, "
          f"duration={st['duration']:.4f}s")
    print(f"  fs_measured(cnt)  : {fs_meas:.4f} S/s  "
          f"（相对设定误差 {abs(fs_meas - fs_hint) / fs_hint * 100:.2f}%）")
    print(f"  fs_from_samples   : {st['fs_from_samples']:.4f} S/s")
    print(f"  轮询/重复读       : n_polls={st['n_polls']}, dup_reads={st['dup_reads']} "
          f"({dup_ratio * 100:.2f}%), new_flag=0 比例={st['new_flag_false_ratio'] * 100:.2f}%")
    print(f"  新样本间隔 min/max: {st['est_interval_min']:.6f} / {st['est_interval_max']:.6f} s")
    print(f"  漏采样本(missed)  : {st['missed_samples']}")
    print(f"对照                : 理论 {regs.FS_THEORY} S/s，2026-10-05 实测 "
          f"{regs.FS_MEASURED_1005} S/s")
    print("=" * 72)
    print("自查结论            : " + ("PASS（fs 估计在 ±5% 内）" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selfcheck())
