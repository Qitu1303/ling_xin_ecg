"""acq_loop.py —— 采集主循环：从 afe_ctrl 搬数、补时间戳、维护环形缓冲，节流产出 Snapshot。

关键约定：0x0C[31] new_flag 是**读清**的，一次判定只能读一次；0x08[31] filt_valid 是单周期
脉冲（约 32 ns），轮询读不到，不作判据。fs 不是常量，一律按 calibrate() 标定值走。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

# 默认不硬依赖同包里的心率模块（可能还没写完），运行时再取
try:
    from .ecg_hr import HrDetector  # type: ignore
    _HAS_HR = True
except Exception:  # pragma: no cover - 心率模块缺席时降级为"只搬数不算心率"
    HrDetector = None  # type: ignore
    _HAS_HR = False

from .regs import CTRL_BYPASS, CTRL_FILT_EN  # noqa: E402  （导出给调用方做控制组合用）


@dataclass
class Snapshot:
    """交给 C 显示模块 / 记录用的一次快照。

    wave: 最近 window_sec 秒样本（uint16，时间正序）；fs: 实测采样率 (S/s)；
    hr: 心率 int，未检出为 None；hr_ok: 是否通过质量判据（False 时 hr 可能是旧值）；
    n_total/n_hw: 软件累计样本数 / 硬件 sample_cnt；dropped: 估算漏样数（负数=重复读）。
    """
    wave: np.ndarray
    fs: float
    hr: Optional[int]
    hr_ok: bool = False
    n_total: int = 0
    n_hw: int = 0
    dropped: int = 0
    t: float = 0.0
    extra: dict = field(default_factory=dict)


class AcquisitionLoop:
    """把 PsAfe 的样本流变成「显示/记录可用的快照」。

    典型用法::
        afe = PsAfe('design_1_wrapper.bit')
        loop = AcquisitionLoop(afe, fs=afe.measure_rate(2.0))   # fs 必须实测
        loop.run(seconds=60, on_snapshot=link.push_snapshot)
    """

    def __init__(self,
                 afe: Any,
                 fs: Optional[float] = None,
                 win_sec: float = 5.0,
                 poll_dt: Optional[float] = None,
                 detector: Any = None,
                 code_min: int = 0,
                 code_max: int = 65535,
                 source: str = 'raw',
                 verbose: bool = True) -> None:
        self.afe = afe
        src = str(source).strip().lower()
        if src not in ('raw', 'filt'):
            raise ValueError(f"source 只能是 'raw' 或 'filt'，收到 {source!r}")
        self.source = src
        self.fs = float(fs) if fs else None
        self.win_sec = float(win_sec)
        self.verbose = bool(verbose)

        # 轮询周期：默认取采样间隔的一半（Nyquist 式的经验值），并夹在 [1ms, 50ms]
        if poll_dt is None:
            if self.fs:
                poll_dt = max(0.001, min(0.05, 0.5 / self.fs))
            else:
                poll_dt = 0.01
        self.poll_dt = float(poll_dt)

        self.detector = detector
        if self.detector is None and _HAS_HR and self.fs:
            # 质量参数按离线分析收紧：3~4 拍定心率太松，无效段也能拿高 q。
            # 不改 HrDetector 默认值 —— tests/test_ecg_hr.py 按默认参数写用例。
            self.detector = HrDetector(fs=self.fs, min_beats=6, quality_min=0.70)

        # ---- 环形缓冲：用 numpy 定长数组而不是 deque，取最近 N 个样本是 O(1) 切片 ----
        self._cap = max(4, int(round((self.fs or 20.0) * self.win_sec)))
        self._buf = np.zeros(self._cap, dtype=np.uint16)
        self._w = 0          # 写指针
        self._n = 0          # 已写入样本数（可用于判断是否绕圈）
        self._code_min = int(code_min)
        self._code_max = int(code_max)

        # ---- 统计 ----
        self.n_loops = 0
        self.n_samples = 0
        self.n_dup = 0            # new_flag 还是 1 但值一模一样：说明轮询比采样快，正常现象
        self.n_missed_polls = 0   # new_flag 为 0 的轮询次数
        self.n_out_of_range = 0   # 码值越界（0 或 65535 满量程），提示前级可能饱和
        self._t0: Optional[float] = None
        self._hw0: Optional[int] = None
        self._last_code: Optional[int] = None
        self._last_t: Optional[float] = None
        # 跨窗 HR 平滑：只影响 _hr() 的显示口径，detector.result 与检测 ok 语义不变。
        # 8 s 窗独立重算会让相邻窗 HR 大幅跳动，显示端数字/-- 高频闪断。
        self._hr_ok_hist: deque = deque(maxlen=5)     # 最近 ok 的 bpm（3 点中位平滑）
        self._hr_last_res = None                      # 上一次检测结果对象（防节流缓存期重复计入）
        self._hr_last_ok_t: Optional[float] = None    # 最近一次 ok 的 monotonic 时刻
        self._hr_last_out: Optional[int] = None       # 上一次输出的 bpm（hold 与限幅基准）
        self.hr_hold_sec = 5.0                        # 失败后保持最后 ok 显示值的时长
        self.hr_slew_bpm = 10.0                       # 相邻显示更新的最大变化（bpm）
        # 有界队列：长跑（>30 分钟）时无限 append 的 list 迟早会变成性能与内存问题
        self._intervals: deque = deque(maxlen=2000)
        self.last_error: Optional[str] = None

        # ---- P1：在线重标定（fs 滑动窗估计）----
        # 负载不同，PS 投递率能从 ~31 掉到 ~21 S/s；只在开机标定一次会把错的时间标尺固化。
        self.fs_fast_sec = 10.0        # 快窗：跟得上负载阶跃（只作触发判据）
        self.fs_slow_sec = 60.0        # 慢窗：重标定取值（抗短时抖动/丢样）
        self.recal_check_sec = 5.0     # 每 5 s 检查一次
        self.recal_tol = 0.03          # 快窗中位 vs 当前 fs 的相对偏差阈值
        self.recal_agree = 0.10        # 快窗/慢窗必须一致到这个程度才动手（见 _maybe_recal）
        self.recal_min_gap = 10.0      # 防抖：两次重标定至少隔 10 s
        self._ts_hist: deque = deque()  # 最近 fs_slow_sec 内每个样本的到达时刻
        self._recal_next_t: Optional[float] = None
        self._recal_last_t: Optional[float] = None
        self.recal_count = 0
        self.recal_last_fs: Optional[float] = None

    # ------------------------------------------------------------------ 内部

    def set_source(self, source: str) -> str:
        """运行中切换取数来源（'raw' / 'filt'），返回生效值。"""
        src = str(source).strip().lower()
        if src not in ('raw', 'filt'):
            raise ValueError(f"source 只能是 'raw' 或 'filt'，收到 {source!r}")
        self.source = src
        return self.source

    def _push(self, code: int) -> None:
        self._buf[self._w] = code
        self._w = (self._w + 1) % self._cap
        self._n += 1
        self.n_samples += 1
        if code <= self._code_min or code >= self._code_max:
            self.n_out_of_range += 1

    def _recent(self) -> np.ndarray:
        """返回按时间正序的最近样本（长度 = min(已收样本数, 缓冲容量)）。"""
        m = int(min(self._n, self._cap))
        if m == 0:
            return np.zeros(0, dtype=np.uint16)
        start = (self._w - m) % self._cap
        if start + m <= self._cap:
            return self._buf[start:start + m].copy()
        return np.concatenate([self._buf[start:], self._buf[: (start + m) % self._cap]])

    # ------------------------------------------------------- P1：在线重标定

    def _fs_est(self, window: float) -> float:
        """最近 window 秒内的**投递率**估计（S/s）：分块时间戳差分，块间取中位。

        必须用滑动窗而非 count/duration：后者是开机累计口径，负载变化后几十秒才追上。
        分块而非逐间隔取中位：到达时刻被轮询网格量化（poll_dt≈16 ms，样本间隔 32~51 ms），
        逐间隔中位会落到量化下沿；分块把量化误差除以块内点数。
        样本不足返回 0.0，调用方据此跳过。
        """
        n = len(self._ts_hist)
        if n < 8:
            return 0.0
        ts = np.fromiter(self._ts_hist, dtype=np.float64, count=n)   # 已按时间升序
        i = int(np.searchsorted(ts, float(ts[-1]) - float(window), side='left'))
        seg = ts[i:]
        if seg.size < 8:
            return 0.0
        edges = np.linspace(0, seg.size, 9).astype(int)              # 8 块（点数等分）
        rates = []
        for a, b in zip(edges[:-1].tolist(), edges[1:].tolist()):
            if b - a < 3:
                continue
            span = float(seg[b - 1] - seg[a])
            if span <= 0.0:
                continue
            rates.append((b - a - 1) / span)
        if len(rates) < 3:
            return 0.0
        return float(np.median(np.asarray(rates, dtype=np.float64)))

    def _maybe_recal(self, now: float) -> None:
        """P1：在线重标定。每 recal_check_sec 检查一次，偏差超阈值才动手。

        触发用快窗（跟得上负载阶跃），取值用慢窗（抗短时抖动/丢样）。
        快慢窗相对差 > recal_agree 时不动手：否则会重标到一个中间值，还白白清空检测窗。
        合理性区间 1~1000 S/s，两次重标定至少隔 recal_min_gap。
        """
        if self.detector is None or not hasattr(self.detector, "set_fs"):
            return
        if self._recal_next_t is not None and now < self._recal_next_t:
            return
        self._recal_next_t = now + float(self.recal_check_sec)

        cur = float(self.fs or 0.0)
        if cur <= 0.0:
            return
        fast = self._fs_est(self.fs_fast_sec)
        if fast <= 0.0 or abs(fast - cur) / cur <= self.recal_tol:
            return                                    # 快窗没意见
        slow = self._fs_est(self.fs_slow_sec)
        if slow <= 0.0 or abs(slow - cur) / cur <= self.recal_tol:
            return                                    # 慢窗还没被新负载占满
        if abs(slow - fast) / max(fast, 1e-9) > self.recal_agree:
            # 快慢窗还打架 = 慢窗仍混着旧负载样本：等两窗一致再一次性改到真值。
            return
        if self._recal_last_t is not None and (now - self._recal_last_t) < self.recal_min_gap:
            return                                    # 防抖
        old = cur
        self.calibrate(rate=slow)                     # 复用既有 calibrate（含区间检查）
        self.recal_count += 1
        self.recal_last_fs = float(self.fs)
        self._recal_last_t = now
        if self.verbose:
            print(f"[acq_loop] P1 在线重标定 #{self.recal_count}：fs {old:.3f} -> {self.fs:.3f} S/s"
                  f"（快窗 {fast:.3f} / 慢窗 {slow:.3f}）", flush=True)

    # ------------------------------------------------------------------ 对外

    def calibrate(self, window: float = 2.0, rate: Optional[float] = None) -> float:
        """实测有效采样率并写入 `self.fs`（同时重建缓冲容量）。

        理论 31.25 与实际 19.53 差 60%，这个错误不报异常，只会让心率每个数字都错。
        rate 给定时跳过 afe.measure_rate() 直接采用：后者是 PL 产样率口径，PS 实际
        投递率可能低 20%+，调用方可用 stats()['est_fs_sw']（软件口径）喂入。
        """
        if rate is None:
            rate = float(self.afe.measure_rate(window=window))
        else:
            rate = float(rate)
        if not (1.0 <= rate <= 1000.0):
            raise ValueError(f"实测采样率 {rate} 不合理，先查采集链路（en=1？XADC 在转？）")
        old_fs = self.fs
        self.fs = rate
        if self.detector is not None and hasattr(self.detector, "set_fs"):
            self.detector.set_fs(rate)               # P1：重建 _cap/_min_samples/_step + 清窗
        elif self.detector is not None and hasattr(self.detector, "fs"):
            # P1：没有 set_fs 的旧检测器（第三方实现）只能退化成逐项重建，口径与 set_fs 一致；
            # 早先这里只改标量，缓冲/最小样本数仍按旧 fs 排布 → 时基混着算。
            self.detector.fs = rate
            rebuild = getattr(self.detector, "_rebuild_fs_dims", None)
            if callable(rebuild):
                rebuild()
            reset = getattr(self.detector, "reset", None)
            if callable(reset):
                reset()
        cap = max(4, int(round(rate * self.win_sec)))
        if cap != self._cap:                     # 容量变了就把老数据平移到新缓冲
            keep = self._recent()
            self._cap = cap
            self._buf = np.zeros(cap, dtype=np.uint16)
            self._buf[: len(keep)] = keep[-cap:]
            self._w = len(keep) % cap
            self._n = len(keep)
        if self.verbose:
            print(f"[acq_loop] 采样率标定：{old_fs} -> {rate:.3f} S/s"
                  f"（缓冲 {cap} 点 ≈ {self.win_sec:g} s）", flush=True)
        return rate

    def tick(self) -> bool:
        """轮询一次。返回 True 表示本次拿到了一个新样本。

        一次调用只读一次 0x0C —— new_flag 是读清的，读两次的第二次必然为 0。
        """
        self.n_loops += 1
        try:
            code, new_flag = self.afe.read_raw()
        except Exception as exc:                 # 读寄存器偶发失败不该让循环死掉
            self.last_error = f"read_raw: {exc!r}"
            return False

        if self._t0 is None:
            self._t0 = time.monotonic()
        if self._hw0 is None:
            try:
                self._hw0 = int(self.afe.read_cnt())
            except Exception:
                self._hw0 = 0

        if not new_flag:
            self.n_missed_polls += 1
            return False

        now = time.monotonic()
        if self._last_t is not None:
            self._intervals.append(now - self._last_t)
        self._last_t = now
        # P1：记下每个样本的到达时刻，供 _fs_est 估投递率（滑动窗，只留慢窗内的点）
        self._ts_hist.append(now)
        t_edge = now - self.fs_slow_sec
        while self._ts_hist and self._ts_hist[0] < t_edge:
            self._ts_hist.popleft()
        self._maybe_recal(now)

        code = int(code) & 0xFFFF
        if self.source == 'filt':
            try:
                code = int(self.afe.read_filt()) & 0xFFFF
            except Exception as exc:
                self.last_error = f"read_filt: {exc!r}"
        if code == self._last_code:
            # 新样本标志还在，但数值没变：低采样率下相邻样本本来就可能是同一个码值，
            # 所以这只能说明"可能重复读了"，不能直接判定为重复 —— 用比例观察即可。
            self.n_dup += 1
        self._last_code = code
        self._push(code)

        # 这里不调用 detector.push(code)：快照路径每次用 process(_recent()) 整窗重建，
        # 喂进去的样本随即被 reset 清掉（白算），额外的重算还会挤占采集线程。
        return True

    def poll_for(self, seconds: float) -> int:
        """忙等 seconds 秒做轮询，返回本次拿到的新样本数。"""
        t_end = time.monotonic() + float(seconds)
        got = 0
        while time.monotonic() < t_end:
            if self.tick():
                got += 1
            time.sleep(self.poll_dt)
        return got

    def _hr(self) -> tuple[Optional[int], bool]:
        if self.detector is None:
            return None, False
        try:
            res = self.detector.process(self._recent())
        except Exception as exc:
            self.last_error = f"detector.process: {exc!r}"
            return None, False
        if res is None:
            return None, False
        hr = getattr(res, "hr_bpm", None)
        ok = bool(getattr(res, "ok", False))
        now = time.monotonic()
        if ok and hr is not None:
            if res is not self._hr_last_res:          # process 有 0.5 s 节流缓存，命中期不重复计入
                self._hr_last_res = res
                self._hr_ok_hist.append(int(round(float(hr))))
                self._hr_last_ok_t = now
                out = int(round(float(np.median(list(self._hr_ok_hist)[-3:]))))
                if self._hr_last_out is not None:
                    dv = out - self._hr_last_out
                    if abs(dv) > self.hr_slew_bpm:
                        out = int(round(self._hr_last_out
                                        + (self.hr_slew_bpm if dv > 0 else -self.hr_slew_bpm)))
                self._hr_last_out = out
            return self._hr_last_out, True
        # 失败：短暂保持最近 ok 的显示值（≤hr_hold_sec），抑制"HR 数字/--"闪断；
        # 超时才如实转失败。检测语义（res.ok / beats）不受影响。
        if (self._hr_last_out is not None and self._hr_last_ok_t is not None
                and (now - self._hr_last_ok_t) <= self.hr_hold_sec):
            return self._hr_last_out, True
        return None, False

    def snapshot(self) -> Snapshot:
        """当前快照（不推进时间，随时可调）。"""
        wave = self._recent()
        hr, hr_ok = self._hr()
        n_hw = self._hw0 or 0
        try:
            n_hw = int(self.afe.read_cnt())
        except Exception as exc:
            self.last_error = f"read_cnt: {exc!r}"
        dropped = 0
        if self._hw0 is not None:
            dropped = (n_hw - self._hw0) - self.n_samples
        extra = self.stats()
        beats = self._beats_meta()                 # 只增不改：R 峰/RR 供周边插件使用
        if beats:
            extra["beats"] = beats
        return Snapshot(wave=wave, fs=float(self.fs or 0.0), hr=hr, hr_ok=hr_ok,
                        n_total=self.n_samples, n_hw=n_hw, dropped=dropped,
                        t=time.monotonic(), extra=extra)

    def _beats_meta(self) -> dict:
        """把检测器最新的拍时刻/RR 序列整理进 extra["beats"]（键可能缺省，消费方用 .get）。

        idx_abs: 绝对样本号；t_s: 相对检测缓冲起点秒值；rr_ms: 漏搏修正后 RR(ms，
        长度可能与 len(t_s)-1 不同)；另有 buf_offset / n_beats / hr_quality。
        锚点 = 结果产出时刻的 (n_samples - 缓冲长度)：process 以 append=False 调用，
        检测器内部 buf_offset 恒为 0，不可直接使用。
        """
        det = self.detector
        if det is None:
            return {}
        try:
            res = getattr(det, "result", None) or getattr(det, "_result", None)
            if res is None:
                return {}
            bidx = tuple(getattr(res, "beat_idx", ()) or ())
            t_s = tuple(getattr(res, "beat_t_s", ()) or ())
            rr_ms = tuple(getattr(res, "rr_ms", ()) or ())
            cache = getattr(self, "_beats_cache", None)
            if cache is None or cache[0] is not res:      # 结果换新 → 重锚一次
                base = int(self.n_samples) - int(len(self._recent()))
                self._beats_cache = (res, base)
                cache = self._beats_cache
            buf_off = int(cache[1])
            if not bidx and not t_s:
                return {}
            return {
                "idx_abs": tuple(buf_off + int(v) for v in bidx),
                "t_s": t_s,
                "rr_ms": rr_ms,
                "buf_offset": buf_off,
                "n_beats": int(getattr(res, "n_beats", len(bidx)) or 0),
                "hr_quality": float(getattr(res, "quality", 0.0) or 0.0),
            }
        except Exception as exc:                    # 绝不因扩展字段影响采集
            self.last_error = f"beats_meta: {exc!r}"
            return {}

    def stats(self) -> dict:
        """自检统计。`dropped` 与 `est_fs` 是判断"到底漏没漏"的两条独立证据。"""
        dur = 0.0
        if self._t0 is not None:
            dur = max(1e-9, time.monotonic() - self._t0)
        est_fs = self.n_samples / dur if dur > 0 else 0.0
        poll_ratio = self.n_loops / dur if dur > 0 else 0.0
        iv = np.asarray(self._intervals, dtype=float) if self._intervals else np.zeros(0)
        return {
            "duration": dur,
            "n_samples": self.n_samples,
            "est_fs_sw": est_fs,                 # 软件侧收数速率（开机累计口径，保持原义）
            "fs_calibrated": self.fs,            # 标定值
            # ---- P1 新增（只增不改，既有字段名一律保留：板端诊断格在用）----
            "fs_sw": self._fs_est(self.fs_fast_sec),   # 快窗（≈10 s）中位投递率
            "fs_used": self.fs,                        # detector 当前在用的 fs
            "recal_count": self.recal_count,           # 在线重标定次数
            "recal_last_fs": self.recal_last_fs,       # 最近一次重标定值（无则 None）
            "poll_hz": poll_ratio,
            "dup_ratio": (self.n_dup / self.n_samples) if self.n_samples else 0.0,
            "missed_poll_ratio": (self.n_missed_polls / self.n_loops) if self.n_loops else 0.0,
            "out_of_range_ratio": (self.n_out_of_range / self.n_samples) if self.n_samples else 0.0,
            "interval_p50": float(np.median(iv)) if iv.size else 0.0,
            "interval_p95": float(np.percentile(iv, 95)) if iv.size else 0.0,
            "last_error": self.last_error,
        }

    def run(self, seconds: float, on_snapshot=None, snapshot_dt: float = 0.05) -> int:
        """跑 seconds 秒：持续轮询，并按 snapshot_dt 节流回调快照。

        on_snapshot 收到的是 `Snapshot`；异常会被吞掉并记进 `last_error` ——
        显示/记录是旁路，不能反过来把采集拖死。
        """
        if self.fs is None:
            self.calibrate()
        t_end = time.monotonic() + float(seconds)
        t_next = 0.0
        while time.monotonic() < t_end:
            self.tick()
            now = time.monotonic()
            if on_snapshot is not None and now >= t_next:
                t_next = now + snapshot_dt
                try:
                    on_snapshot(self.snapshot())
                except Exception as exc:
                    self.last_error = f"on_snapshot: {exc!r}"
            time.sleep(self.poll_dt)
        return self.n_samples

    # ------------------------------------------------------------------ 记录

    def dump_csv(self, path: str, snap: Optional[Snapshot] = None) -> str:
        """把最近一段波形写成 CSV（码值 + 电压），供离线调参与留证。"""
        snap = snap or self.snapshot()
        arr = np.asarray(snap.wave, dtype=np.uint16).astype(np.int64)
        volt = arr / 65536.0 / 0.301
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("index,code,volt_V\n")
            for i, (c, v) in enumerate(zip(arr.tolist(), volt.tolist())):
                f.write(f"{i},{c},{v:.6f}\n")
        if self.verbose:
            print(f"[acq_loop] 已导出 {len(arr)} 点 -> {path}", flush=True)
        return path
