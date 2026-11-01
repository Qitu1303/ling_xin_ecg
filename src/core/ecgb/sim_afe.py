"""sim_afe.py —— 模拟 AFE 信号源（比赛演示用；**不是硬件后端**，波形由 sim_ecg 合成）。

接口与 PsAfe 同构，`_ol`/`ip` 为 None 让 board_demo 走"不推屏"分支，播完即静默不回绕。
关键约束：投递率锁死（到点才 True、不补发不跳发，read_cnt() 返回已投递数）→ fs_sw ≈ fs_used
≈ --sim-fs，P1 重标定一次都不触发（唯一例外是真停顿的快进丢弃）。
"""
from __future__ import annotations

import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .sim_ecg import make_ecg, truth_hr_bpm

__all__ = ["SimAfe", "parse_sim_cli", "banner", "print_cases", "run_selftest", "CASES"]

CODE_MID = 32768.0

# 停顿快进阈值（秒）：到期样本累计欠账 ≥ 该值才认"真停顿"。按秒存是为了随 --sim-fs 缩放
# （0.25 s ≈ 7.8 个采样间隔 @31.25 S/s）。稳态下 now-due < 1 个采样间隔、攒不起欠账，
# 只有主循环被堵住才会触发；0.25 s 仍比轮询抖动（≤ 1 个 poll_dt）高一个半数量级。
_STALL_FASTFWD_SEC = 0.25

_BANNER = (
    "\n" + "!" * 72 + "\n"
    "[SimAfe] 本次数据来自 **合成心电（sim_ecg.make_ecg）**，不是真实人体信号。\n"
    "         可用于演示/回归，**不可**用于上板结论或任何诊断判断。\n"
    "         真板采集请去掉 --source sim（缺省即真板路径）。\n"
    + "!" * 72 + "\n"
)

# 演示剧本表（--sim-list 打印这一列；顺序即推荐演示顺序）
CASES: Dict[str, Tuple[str, str]] = {
    "normal": ("稳态 --sim-hr（默认 72 bpm），噪声 --sim-noise 码",
               "零事件（含 device_suspect）；status=窦性节律"),
    "device_missed": ("每第 3 拍 R 峰幅值塌陷到 0.35×；存活拍按 1.0/0.35 **交替调制**"
                      "（模拟 31 S/s 采样相位让 R 峰幅值拍间大幅波动 → 叠加设备塌陷拍"
                      "被检测器漏检）",
                      "missed_suspect(low)；不得出 missed / brady / pause"),
    "missed": ("RR 直接翻倍（少一拍），幅值正常", "missed(low)，不得变 suspect"),
    "pause": ("RR = 2.6 s（--sim-sev 2 → 3.4 s）", "pause medium / high，≤1 s 上卡片"),
    "brady": ("异常段稳态 45 bpm", "brady medium"),
    "tachy": ("异常段稳态 130 bpm（--sim-sev 2 → 160 bpm）", "tachy medium / high"),
    "pac": ("RR 循环 0.62 / 0.44 / 0.78 / 0.67 s", "irregular medium"),
    "afib": ("RR 随机 0.5~1.1 s（--sim-seed 固定，可复现）", "irregular medium"),
}

_INJECT_NOTE = ("注入时刻 --sim-at 必须 ≥ 预热 5 s + 8 s 检测窗（默认 12 s）；"
                "否则注入会被 board_demo 的预热窗吞掉")


def banner() -> str:
    return _BANNER


# ----------------------------------------------------------------------------
# 参数解析（照 base.parse_plugin_cli 的风格；缺省零侵入）
# ----------------------------------------------------------------------------
_SIM_OPTS: Dict[str, Tuple[str, Any]] = {
    "--sim-case": ("pattern", str),
    "--sim-fs": ("fs_deliver", float),
    "--sim-sec": ("seconds", float),
    "--sim-hr": ("base_hr", float),
    "--sim-at": ("at", float),
    "--sim-dur": ("dur", float),
    "--sim-amp": ("amp", float),
    "--sim-noise": ("noise", float),
    "--sim-seed": ("seed", int),
    "--sim-sev": ("sev", int),
    "--sim-jitter": ("jitter", float),
}


def parse_sim_cli(argv: List[str]) -> Tuple[List[str], Dict[str, Any]]:
    """从 argv 里剔除 `--source sim` 与 `--sim-*`，返回 (剩余 argv, 模拟源参数)。

    返回的第二项为空字典 ⇒ 调用方必须走原来的 PsAfe 路径（行为一行不变）。
    控制位（`_sim/_list/_selftest`）是给 board_demo 用的，不能直接喂给 SimAfe()。
    """
    rest: List[str] = []
    opts: Dict[str, Any] = {}
    is_sim = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--source":
            src = argv[i + 1] if i + 1 < len(argv) else ""
            is_sim = str(src).strip().lower() == "sim"
            i += 2
            continue
        if a == "--sim-list":
            opts["_list"] = True
            i += 1
            continue
        if a == "--selftest":
            opts["_selftest"] = True
            i += 1
            continue
        if a in _SIM_OPTS:
            key, typ = _SIM_OPTS[a]
            if i + 1 >= len(argv):
                raise ValueError(f"{a} 缺少取值")
            opts[key] = typ(argv[i + 1])
            i += 2
            continue
        rest.append(a)
        i += 1
    if not is_sim:
        return rest, {}                    # 没给 --source sim ⇒ 完全零侵入
    opts["_sim"] = True
    return rest, opts


def print_cases(out=None) -> None:
    """--sim-list：打印剧本表（含"注入时刻"纪律）。"""
    fh = out or sys.stdout
    print("SimAfe 演示剧本（--sim-case）：", file=fh)
    for name, (how, want) in CASES.items():
        print(f"  {name:<14} 注入：{how}", file=fh)
        print(f"  {'':<14} 期望：{want}", file=fh)
    print("提示：" + _INJECT_NOTE, file=fh)
    print("用法：python3 board_demo.py <bit> . 180 --source sim --sim-case pause "
          "--sim-at 15 --plugins all --plugins-file plugins.json", file=fh)


# ----------------------------------------------------------------------------
# 虚拟时钟（自检用；板上/离线回归都靠它把 20 s 压成毫秒级）
# ----------------------------------------------------------------------------
class _VClock:
    """单调虚拟时钟：sleep 只推进虚拟时间，不真睡。"""

    def __init__(self, t0: float = 1000.0) -> None:
        self.t = float(t0)

    def monotonic(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += max(float(dt), 0.0)


# ----------------------------------------------------------------------------
# 模拟 AFE
# ----------------------------------------------------------------------------
class SimAfe:
    """与 PsAfe 同构的模拟信号源（鸭子类型，见模块头的方法清单）。

    pattern: 剧本名（CASES 的键）；fs_deliver: 投递采样率 S/s（21.0 是时基压测剧本）。
    seconds: 合成缓冲总时长，不设回绕、播完即静默；base_hr/at/dur/sev: 基率与注入窗。
    amp/noise: R 峰幅值与噪声（码值）；seed: 噪声/漂移/afib 的 RR 都由此决定，保证可复现。
    jitter: 投递时刻抖动（采样间隔的比例，**默认必须为 0**，否则 A 门会判成设备嫌疑）。
    """

    backend = "sim_ecg"          # 日志/自检判读：这不是硬件
    _ol = None                   # board_demo: getattr(afe,'_ol',None) → 不推屏
    ip = None                    # checkup 用 afe.ip.read：sim 必须跳过自检

    def __init__(self, pattern: str = "normal", fs_deliver: float = 31.25,
                 seconds: float = 300.0, base_hr: float = 72.0,
                 at: float = 12.0, dur: float = 20.0, amp: float = 1500.0,
                 noise: float = 10.0, seed: int = 7, sev: int = 1,
                 jitter: float = 0.0, verbose: bool = True,
                 clock: Optional[Any] = None) -> None:
        self.pattern = str(pattern or "normal").strip().lower()
        if self.pattern not in CASES:
            raise ValueError(f"未知 --sim-case：{pattern!r}；可选：{list(CASES)}")
        self.fs_deliver = float(fs_deliver)
        if not (1.0 <= self.fs_deliver <= 1000.0):
            raise ValueError(f"--sim-fs 不合理：{fs_deliver}")
        self.seconds = float(seconds)
        self.base_hr = float(base_hr)
        self.at = float(at)
        self.dur = float(dur)
        self.amp = float(amp)
        self.noise = float(noise)
        self.seed = int(seed)
        self.sev = int(sev)
        self.jitter = max(0.0, float(jitter))
        self.verbose = bool(verbose)
        # clock 可以是"可调用返回秒"（对齐 PsAfe 的 clock 参数），也可以是带
        # monotonic()/sleep() 的时钟对象（_VClock：虚拟时间，自检用）
        clk_obj = clock
        if clk_obj is not None and hasattr(clk_obj, "monotonic"):
            self._clock = clk_obj.monotonic
            self._sleep_fn = clk_obj.sleep if hasattr(clk_obj, "sleep") else time.sleep
        else:
            self._clock = clk_obj or time.monotonic
            self._sleep_fn = time.sleep
        self._band: Optional[int] = None
        self._filt_en = False
        self._running = False
        self._emitted = 0
        # 只增计数：因真停顿被"快进丢弃"的样本数（稳态恒 0；见 read_raw）
        self.skipped = 0
        self._t_anchor: Optional[float] = None
        self._dt = 1.0 / self.fs_deliver
        self._jrng = np.random.default_rng(int(seed) + 991)

        if self.verbose:
            print(_BANNER, flush=True)
        self._beats, self._amps = self._build_beats()
        self._sig, self._truth = make_ecg(
            self.fs_deliver, self.seconds, self.base_hr,
            amp=self.amp, noise=self.noise, seed=self.seed,
            beat_times=self._beats, beat_amp=self._amps, return_truth=True)
        self._n = int(self._sig.size)
        self._last = int(self._sig[0]) if self._n else int(CODE_MID)

    # ------------------------------------------------------------ 剧本合成
    def _build_beats(self) -> Tuple[List[float], List[float]]:
        """按剧本生成 (R 峰时刻, 逐拍幅值倍数)。

        所有异常都表达成 RR 序列 / 幅值倍数，不改合成器内部逻辑：
          · 少一拍 / 停搏 / 房早 / 过速 / 过缓 → RR 序列；
          · 设备漏检（幅值塌陷）→ beat_amp 倍数（波形里那拍仍在 → E2/E3 能取证）。
        """
        rr0 = 60.0 / self.base_hr
        rng = np.random.default_rng(self.seed + 17)
        pac_cycle = [0.62, 0.44, 0.78, 0.67, 0.62]
        times: List[float] = []
        amps: List[float] = []
        t = 0.5 * rr0
        k = 0
        paused = False
        while t < self.seconds - 0.1:
            in_win = self.at <= t < (self.at + self.dur)
            rr = rr0
            amp = 1.0
            if in_win:
                if self.pattern == "device_missed":
                    # 塌陷 0.35 + 存活拍交替调制 1.0/0.35：单独塌陷时上游检测器根本不漏检，加调制
                    # （还原 31 S/s 采样相位下 R 峰幅值拍间波动）后塌陷拍才会被丢掉。0.40 会被 reset
                    # 后的自适应阈值收编；0.30 会连丢 3 拍出 pause（违反剧本），故取 0.35。
                    if k % 3 == 0:
                        amp = 0.35
                    elif (k // 3) % 2 == 0:
                        amp = 0.35
                elif self.pattern == "missed":
                    if k % 4 == 3:
                        rr = 2.0 * rr0          # 少一拍：RR 翻倍，幅值正常
                elif self.pattern == "pause":
                    if not paused:
                        rr = 3.4 if self.sev >= 2 else 2.6
                        paused = True
                elif self.pattern == "brady":
                    rr = 60.0 / 45.0
                elif self.pattern == "tachy":
                    rr = 60.0 / (160.0 if self.sev >= 2 else 130.0)
                elif self.pattern == "pac":
                    rr = pac_cycle[k % len(pac_cycle)]
                elif self.pattern == "afib":
                    rr = float(rng.uniform(0.5, 1.1))
            times.append(t)
            amps.append(amp)
            t += float(np.clip(rr, 0.3, 4.0))
            k += 1
        return times, amps

    # ------------------------------------------------------------ 展示辅助
    def summary(self) -> str:
        hr = truth_hr_bpm(self._truth, self.fs_deliver)
        how, want = CASES[self.pattern]
        lines = [
            f"[SimAfe] case={self.pattern}  注入：{how}",
            f"[SimAfe] 合成 {self.seconds:.0f} s @ {self.fs_deliver:.2f} S/s → "
            f"{self._n} 点；真值 {len(self._truth)} 拍，真值中位 HR "
            f"{('%.1f' % hr) if hr else '--'} bpm",
            f"[SimAfe] 异常注入窗 {self.at:.1f}~{self.at + self.dur:.1f} s"
            f"（sev={self.sev}）；期望节律卡：{want}",
            f"[SimAfe] 缓冲播完即静默（不回绕）；{_INJECT_NOTE}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------ afe 接口
    def start(self, filt_en: bool = False, bypass: bool = False,
              band: Optional[int] = None) -> int:
        """使能投递（对齐 PsAfe.start 的签名，允许 kwargs）。返回 0（sim 无控制字）。"""
        self._filt_en = bool(filt_en)
        if band is not None:
            self.set_band(band)
        self._running = True
        self._emitted = 0
        self._t_anchor = float(self._clock())
        return 0

    def stop(self) -> None:
        """停投递：之后 read_raw() 恒 (last, False)（收尾不再产生假事件）。"""
        self._running = False

    def reset_ctrl(self) -> None:
        """sim 无寄存器可复位（保持接口对称）。"""
        return None

    def set_band(self, band: int) -> None:
        b = int(band)
        if not 0 <= b <= 3:
            raise ValueError(f"带宽档位只能是 0..3，收到 {band}")
        self._band = b

    @property
    def band(self) -> Optional[int]:
        return self._band

    def read_cnt(self) -> int:
        """**已投递**样本数（不是生成数）→ 稳态下 snapshot 的 dropped 恒 ≈0。

        停顿快进丢掉的欠账也计入（硬件 sample_cnt 口径是"产了多少"），所以真停顿之后
        dropped 会如实变成正数；要区分丢的是什么，看 stats() 的 skipped。
        """
        return int(self._emitted)

    def read_filt(self) -> int:
        """滤波输出 = 当前码值（sim_ecg 已含线性相位 AAF，等价滤波结果）。"""
        return int(self._last) & 0xFFFF

    def read_raw(self) -> Tuple[int, bool]:
        """到点才返回 True；一次调用最多推进 1 个样本（**不补发**，对齐 0x0C 读清语义）。

        **停顿快进**：若到期时刻已落后当前时刻超过 _STALL_FASTFWD_SEC，说明主循环真被堵住
        （如插件 on_start 建档约 0.4 s），此时丢掉欠账样本、把时标跳到当前应在位置并返回 False。
        必须丢而不是追发：追发会让随后的实测投递率假性升高，P1 因此重标定并 reset 检测窗，
        正落在注入窗内 → 演示断片。稳态下 now-due 最多一个 poll_dt，永不触发。
        """
        if not self._running or self._n == 0:
            return int(self._last) & 0xFFFF, False
        if self._t_anchor is None:
            self._t_anchor = float(self._clock())
        if self._emitted >= self._n:
            return int(self._last) & 0xFFFF, False       # 播完即静默（不回绕）
        now = float(self._clock())
        due = self._t_anchor + (self._emitted + 1) * self._dt
        if self.jitter > 0.0:
            due += float(self._jrng.uniform(-1.0, 1.0)) * self.jitter * self._dt
        lag = now - due
        if lag + 1e-9 < 0.0:
            return int(self._last) & 0xFFFF, False       # 还没到点（稳态走这里）
        # 到点了：先判"是不是真停顿"。欠账 ≥ 阈值 ⇒ 丢掉欠账、把时标跳到"当前应在的
        # 位置"，本次不交付。阈值下限 4×dt 是给极低投递率（--sim-fs 接近 1 S/s）留的余量。
        thr = max(float(_STALL_FASTFWD_SEC), 4.0 * self._dt)
        if lag >= thr:
            skip = int((now - self._t_anchor) / self._dt) - int(self._emitted)
            if skip > 0:
                self.skipped += int(skip)
                self._emitted = min(int(self._emitted) + int(skip), self._n)
                if self.verbose:
                    print(f"[SimAfe] 停顿快进：丢掉 {skip} 个欠账样本"
                          f"（now-due={lag:.3f} s ≥ 阈值 {thr:.3f} s）；"
                          f"累计 skipped={self.skipped}", flush=True)
            return int(self._last) & 0xFFFF, False
        code = int(self._sig[self._emitted])
        self._emitted += 1
        self._last = code
        return code & 0xFFFF, True

    def stats(self) -> Dict[str, Any]:
        """投递链路自检计数（纯新增方法，无人调用也不影响鸭子类型）。

        skipped: 因真停顿被快进丢掉的样本数（稳态恒 0）。read_cnt() 与 stats()["delivered"]
        是同一个数：硬件口径是"产了多少"，丢样要能被 dropped 看见。
        """
        return {
            "delivered": int(self._emitted),
            "skipped": int(self.skipped),
            "generated": int(self._n),
            "fs_deliver": float(self.fs_deliver),
            "dt": float(self._dt),
            "stall_fastfwd_sec": float(_STALL_FASTFWD_SEC),
        }

    def measure_rate(self, window: float = 2.0) -> float:
        """真睡 window 秒，返回**实测投递率**（S/s）。默认 ≈ --sim-fs。

        这条命令本身就是投递链路自检：read_raw 节拍逻辑若写错（补发/跳发）这里立刻不对。
        """
        if window <= 0:
            raise ValueError(f"window 必须为正：{window}")
        t_begin = float(self._clock())
        t_end = t_begin + float(window)
        got = 0
        t_first = 0.0
        t_last = 0.0
        self._running = True
        if self._t_anchor is None:
            self._t_anchor = float(self._clock())
        poll = min(self._dt * 0.25, 0.004)
        while True:
            now = float(self._clock())
            if now >= t_end:
                break
            if self.read_raw()[1]:
                if not got:
                    t_first = now
                t_last = now
                got += 1
            self._sleep_fn(poll)
        # 口径：**首末两次成功投递之间的实测斜率**，不是 got/window —— 后者会被窗口边界的
        # 半个采样间隔拉低 4%，让 P1 误判成投递不足并重标定。
        if got >= 2 and (t_last - t_first) > 0.0:
            return (got - 1) / (t_last - t_first)
        return got / float(window)


# ----------------------------------------------------------------------------
# 自检（--selftest）：用真 AcquisitionLoop 跑 20 s 虚拟时间
# ----------------------------------------------------------------------------
def run_selftest(seconds: float = 20.0, out=None) -> int:
    """判据：n_samples ≈ 625（=20 s×31.25）、fs_sw/fs_used ≈ 1.00、recal_count == 0。"""
    fh = out or sys.stdout
    from .acq_loop import AcquisitionLoop
    import ecgb.acq_loop as _acq

    clk = _VClock()
    old_time = _acq.time
    _acq.time = clk                        # 只替换采集循环内的 time 垫片（进程内）
    try:
        afe = SimAfe(pattern="normal", clock=clk, verbose=False)
        rc = afe.start()
        fs_hw = afe.measure_rate(window=1.0)
        loop = AcquisitionLoop(afe, fs=fs_hw, win_sec=8.0, verbose=False)
        loop.run(seconds=float(seconds), on_snapshot=None)
        st = loop.stats()
        snap = loop.snapshot()             # 取 snapshot 口径的 dropped（主循环那行打印的就是它）
    finally:
        _acq.time = old_time
    n = int(st["n_samples"])
    fs_sw = float(st["fs_sw"] or 0.0)
    fs_used = float(st["fs_used"] or 0.0)
    ratio = (fs_sw / fs_used) if fs_used else 0.0
    print(f"[selftest] start() 返回 {rc}；fs_hw(measure_rate 1 s) = {fs_hw:.3f} S/s", file=fh)
    print(f"[selftest] 虚拟 {seconds:.0f} s 后：n_samples = {n}"
          f"（期望 ≈{int(round(seconds * 31.25))}）", file=fh)
    print(f"[selftest] fs_sw = {fs_sw:.3f} / fs_used = {fs_used:.3f}"
          f" → 比值 {ratio:.4f}", file=fh)
    print(f"[selftest] recal_count = {int(st['recal_count'])}"
          f"；快照 dropped = {int(snap.dropped)}（read_cnt 恒等于已投递数 → 不会出现负数）",
          file=fh)
    ok = (abs(n - seconds * 31.25) <= max(6.0, 0.02 * seconds * 31.25)
          and abs(ratio - 1.0) < 0.02
          and int(st["recal_count"]) == 0)
    print("[selftest] " + ("通过 ✔（投递率锁死、P1 不触发）" if ok else "不通过 ✘"), file=fh)
    return 0 if ok else 1


if __name__ == "__main__":                 # python -m ecgb.sim_afe --list / --selftest
    args = [a for a in sys.argv[1:]]
    if "--list" in args or "--sim-list" in args:
        print_cases()
        raise SystemExit(0)
    if "--selftest" in args:
        raise SystemExit(run_selftest())
    print_cases()
    print("\n提示：加 --selftest 跑 20 s 虚拟时间自检。")
