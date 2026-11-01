"""checkup.py —— 板上「一次跑完就出结论」的联调自检（8 关，逐关 PASS/FAIL/WARN/SKIP）。

关与关之间的依赖是**硬**的：第 1/2 关不过后面全 SKIP；判据里的数字全部来自实测或 regs.py
的常量；任何一关崩了只记 traceback 并继续下一关。用法: run_checkup(bit_path, sim, out_dir, seconds)。
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from .acq_loop import AcquisitionLoop, Snapshot
from .regs import (
    CTRL_EN, CTRL_FILT_EN, CTRL_RAW_PASS, CTRL_FILTERED, CTRL_BYPASSED,
    FS_THEORY, code_to_volt,
)

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


def _force_utf8_when_piped() -> None:
    """Windows 上 stdout 被重定向时默认 GBK，打印 ✅/❌ 会抛 UnicodeEncodeError。

    非 tty（被管道/文件接走）时强制 UTF-8 —— 联调时最不该出现工具自己先崩。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


@dataclass
class Stage:
    """一关的结果。`gating=True` 表示不过就别往下走了（后面全 skip）。"""
    name: str
    status: str = SKIP
    detail: str = ""
    evidence: dict = field(default_factory=dict)
    gating: bool = False
    error: str = ""


@dataclass
class CheckupReport:
    stages: list = field(default_factory=list)
    fs_used: float = 0.0
    t_start: float = field(default_factory=time.time)

    def add(self, name: str, status: str, detail: str = "", gating: bool = False, **evidence) -> Stage:
        st = Stage(name=name, status=status, detail=detail, evidence=dict(evidence), gating=gating)
        self.stages.append(st)
        print(f"[{status:4s}] {name} —— {detail}", flush=True)
        return st

    @property
    def failed(self) -> list:
        return [s for s in self.stages if s.status == FAIL]

    @property
    def ok(self) -> bool:
        return not self.failed

    def print_table(self) -> str:
        w = max([len(s.name) for s in self.stages] + [12])
        lines = ["", "=" * (w + 40), f"{'关卡'.ljust(w)}  状态   结论", "-" * (w + 40)]
        for s in self.stages:
            lines.append(f"{s.name.ljust(w)}  {s.status:4s}  {s.detail}")
        lines.append("-" * (w + 40))
        n_fail = len(self.failed)
        lines.append(f"总计 {len(self.stages)} 关：PASS {sum(s.status==PASS for s in self.stages)} / "
                     f"FAIL {n_fail} / WARN {sum(s.status==WARN for s in self.stages)} / "
                     f"SKIP {sum(s.status==SKIP for s in self.stages)}")
        lines.append("结论：" + ("✅ 采集链路通过，可以往心率/显示走" if n_fail == 0
                                else f"❌ 有 {n_fail} 关不过，先解决最先 FAIL 的那一关，别一次改两处"))
        lines.append("=" * (w + 40))
        out = "\n".join(lines)
        print(out, flush=True)
        return out


def _median_rate(afe: Any, window: float) -> float:
    return float(afe.measure_rate(window=window))


def run_checkup(bit_path: Optional[str] = None,
                sim: Any = None,
                out_dir: Optional[str] = None,
                seconds: float = 10.0,
                fs_window: float = 3.0,
                detect_hr: bool = True,
                verbose: bool = True,
                afe: Any = None) -> CheckupReport:
    """跑完整 8 关自检。

    bit_path: 板上 .bit 路径；给 None 时 PsAfe 会尝试降级（MMIO → sim）。
    sim:      离线模拟器（FakeAfe）或假 overlay；给了就不碰真实硬件。
    out_dir:  产物目录（波形 CSV、报告 txt），None 则不落盘；detect_hr: 是否跑第 8 关。
    """
    from .ps_afe import PsAfe  # 延迟导入，方便本模块被单独测试

    _force_utf8_when_piped()
    rep = CheckupReport()
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # ---------------- 第 0 关：建立连接 ----------------
    try:
        if afe is None:
            afe = PsAfe(bit_path=bit_path, sim=sim, verbose=verbose)
        who = getattr(afe, "backend", "?")
        rep.add("0 建立连接", PASS, f"后端 = {who}", gating=True, backend=who)
    except Exception:
        rep.add("0 建立连接", FAIL, "PsAfe 建不起来：见 error", gating=True,
                error=traceback.format_exc())
        rep.print_table()
        return rep

    def skip_rest(reason: str) -> None:
        for nm in ("1 写读自检", "2 采样率标定", "3 en=0 冻结", "4 原始样本在动",
                   "5 滤波输出合理", "6 旁路对照", "7 采波形存盘", "8 心率"):
            if not any(s.name == nm for s in rep.stages):
                rep.add(nm, SKIP, reason, gating=(nm.startswith(("1", "2"))))

    # ---------------- 第 1 关：AXI 写读自检 ----------------
    try:
        before = afe.ip.read(0x00) if hasattr(afe, "ip") and afe.ip is not None else None
        afe.start(filt_en=False)                    # 写 0x1
        back = afe.ip.read(0x00)
        if (back & CTRL_EN) != CTRL_EN:
            rep.add("1 写读自检", FAIL, f"0x00 回读 {back:#x}，en 位没写进去", gating=True,
                    write=CTRL_EN, read=back, before=before)
            skip_rest("第 1 关不过")
            rep.print_table()
            return rep
        rep.add("1 写读自检", PASS, f"0x00 回读 {back:#x}，en 位可写", gating=True,
                write=CTRL_EN, read=back)
    except Exception:
        rep.add("1 写读自检", FAIL, "读写 0x00 抛异常", gating=True, error=traceback.format_exc())
        skip_rest("第 1 关不过")
        rep.print_table()
        return rep

    # ---------------- 第 2 关：采样率标定 ----------------
    fs = 0.0
    try:
        c0 = afe.read_cnt()
        t0 = time.monotonic()
        time.sleep(fs_window)
        c1 = afe.read_cnt()
        dt = time.monotonic() - t0
        fs = (c1 - c0) / dt
        rep.fs_used = fs
        drift = (fs / FS_THEORY - 1.0) * 100.0 if FS_THEORY else 0.0
        ev = dict(cnt0=c0, cnt1=c1, dt=dt, fs_measured=fs, fs_theory=FS_THEORY,
                  deviation_pct=drift)
        if fs < 1.0:
            rep.add("2 采样率标定", FAIL,
                    f"实测 {fs:.3f} S/s（计数 {c0}->{c1}），链路没在跑", gating=True, **ev)
            skip_rest("没有样本流")
            rep.print_table()
            return rep
        status = PASS if abs(drift) <= 5 else WARN
        detail = f"实测 {fs:.3f} S/s（理论 {FS_THEORY}，偏差 {drift:+.1f}%）"
        if status == WARN:
            detail += " → 与理论不符，确认是不是 Q-01 那个已知缺口"
        rep.add("2 采样率标定", status, detail, gating=True, **ev)
    except Exception:
        rep.add("2 采样率标定", FAIL, "测速抛异常", gating=True, error=traceback.format_exc())
        skip_rest("没有样本流")
        rep.print_table()
        return rep

    # ---------------- 第 3 关：en=0 计数冻结（验证 CONVST 真的由我们控制） ----------------
    try:
        afe.stop()
        d0 = afe.read_cnt()
        time.sleep(1.0)
        d1 = afe.read_cnt()
        delta = d1 - d0
        if delta == 0:
            rep.add("3 en=0 冻结", PASS, "en=0 时计数不动，采样由 CONVST 定拍")
        else:
            rep.add("3 en=0 冻结", WARN,
                    f"en=0 期间仍增 {delta} → XADC 可能没跑在事件模式（查触发模式与 CONVST 连接）",
                    delta=delta)
        afe.start(filt_en=False)
    except Exception:
        rep.add("3 en=0 冻结", WARN, "该关抛异常", error=traceback.format_exc())

    # ---------------- 第 4 关：原始样本在动 + 电压换算 ----------------
    raw_snap = None
    try:
        loop = AcquisitionLoop(afe, fs=fs, win_sec=max(4.0, seconds / 2), verbose=verbose)
        loop.poll_for(max(2.0, seconds / 3))
        raw_snap = loop.snapshot()
        arr = np.asarray(raw_snap.wave)
        n = arr.size
        uniq = int(np.unique(arr).size) if n else 0
        p2p = int(arr.max() - arr.min()) if n else 0
        mid = float(np.median(arr)) if n else 0.0
        ev = dict(n=n, uniq=uniq, p2p=p2p, median_code=mid,
                  median_volt=code_to_volt(mid) if n else 0.0, dropped=raw_snap.dropped,
                  **{k: v for k, v in loop.stats().items() if k in
                     ("est_fs_sw", "dup_ratio", "missed_poll_ratio", "out_of_range_ratio")})
        if n < 4:
            rep.add("4 原始样本在动", FAIL, f"只收到 {n} 个样本，采集没跑起来", gating=False, **ev)
        elif uniq <= 2:
            rep.add("4 原始样本在动", FAIL,
                    f"收到 {n} 点但只有 {uniq} 个不同码值（常见原因：A0 恒 0 → 查 DRP 读时序）", **ev)
        else:
            rep.add("4 原始样本在动", PASS,
                    f"{n} 点 / {uniq} 个不同码值 / 峰峰 {p2p} LSB / 中值 {mid:.0f}"
                    f"（≈{code_to_volt(mid):.3f} V）", **ev)
    except Exception:
        rep.add("4 原始样本在动", FAIL, "采集抛异常", error=traceback.format_exc())

    # ---------------- 第 5 关：滤波输出合理（与原始对照） ----------------
    try:
        afe.start(filt_en=True)
        loop_f = AcquisitionLoop(afe, fs=fs, win_sec=max(4.0, seconds / 2), verbose=False)
        loop_f.poll_for(max(2.0, seconds / 3))
        snap_f = loop_f.snapshot()
        f = np.asarray(snap_f.wave)
        ok = f.size >= 4 and int(np.unique(f).size) > 2
        detail = (f"滤波输出 {f.size} 点 / {int(np.unique(f).size)} 个不同码值 / 中值 {np.median(f):.0f}"
                  if f.size else "滤波输出为空")
        rep.add("5 滤波输出合理", PASS if ok else FAIL, detail,
                n=int(f.size), median=float(np.median(f)) if f.size else 0.0)
        if raw_snap is not None and raw_snap.wave.size and f.size:
            a = np.asarray(raw_snap.wave, dtype=float)
            b = np.asarray(f, dtype=float)
            m = min(a.size, b.size)
            a, b = a[-m:], b[-m:]
            # 常数序列（全 0 / 满量程卡死）求相关系数是 0/0 → NaN + RuntimeWarning，
            # 会先糊满日志再误导判读，所以自己先判掉。
            const = a.std() < 1e-9 or b.std() < 1e-9
            corr = None if (m < 2 or const) else float(np.corrcoef(a, b)[0, 1])
            rep.add("5b 滤波前后相关性", WARN if const else (PASS if m >= 4 else WARN),
                    ("其中一路是常数序列（前级可能饱和或卡死），相关性无意义"
                     if const else f"相关系数 {corr:+.3f}（滤波会改幅度/相位，只看是不是完全无关）"),
                    corr=corr)
    except Exception:
        rep.add("5 滤波输出合理", FAIL, "滤波链路读取抛异常", error=traceback.format_exc())

    # ---------------- 第 6 关：旁路对照（M2 演示开关） ----------------
    try:
        afe.start(bypass=True)
        time.sleep(0.3)
        loop_b = AcquisitionLoop(afe, fs=fs, win_sec=2.0, verbose=False)
        loop_b.poll_for(1.0)
        sb = loop_b.snapshot()
        b = np.asarray(sb.wave)
        lo, hi = (int(b.min()), int(b.max())) if b.size else (0, 0)
        rep.add("6 旁路对照", PASS if b.size >= 4 else WARN,
                f"bypass=1 时读到 {b.size} 点，范围 [{lo}, {hi}]（应与原始样本接近）",
                n=int(b.size), min=lo, max=hi)
        afe.start(filt_en=True)   # 回到滤波态
    except Exception:
        rep.add("6 旁路对照", WARN, "该关抛异常", error=traceback.format_exc())

    # ---------------- 第 7 关：采一段波形存盘 ----------------
    if out_dir:
        try:
            loop7 = AcquisitionLoop(afe, fs=fs, win_sec=seconds, verbose=False)
            loop7.poll_for(seconds)
            s7 = loop7.snapshot()
            csv = os.path.join(out_dir, "wave_raw.csv")
            loop7.dump_csv(csv, s7)
            st = loop7.stats()
            rep.add("7 采波形存盘", PASS if s7.wave.size >= 4 else FAIL,
                    f"{s7.wave.size} 点 -> {os.path.basename(csv)}；"
                    f"软件侧 {st['est_fs_sw']:.2f} S/s vs 标定 {fs:.2f} S/s",
                    path=csv, n=int(s7.wave.size), est_fs_sw=st["est_fs_sw"],
                    dropped=s7.dropped)
        except Exception:
            rep.add("7 采波形存盘", WARN, "存盘抛异常", error=traceback.format_exc())

    # ---------------- 第 8 关：心率 ----------------
    if detect_hr:
        try:
            from .ecg_hr import HrDetector
            det = HrDetector(fs=fs)
            loop8 = AcquisitionLoop(afe, fs=fs, win_sec=max(10.0, seconds), detector=det, verbose=False)
            loop8.poll_for(max(8.0, seconds))
            s8 = loop8.snapshot()
            hr, ok = s8.hr, s8.hr_ok
            if hr is None:
                rep.add("8 心率", WARN,
                        "未检出心率（无信号源 / A0 悬空时属正常；接上 1~2 Hz 信号源再看）",
                        n=int(s8.wave.size))
            else:
                rep.add("8 心率", PASS if ok else WARN,
                        f"HR = {hr} bpm（质量 {'可信' if ok else '偏低，需信号源确认'}）",
                        hr=hr, ok=ok, n=int(s8.wave.size))
        except ImportError:
            rep.add("8 心率", SKIP, "ecg_hr 模块不在（还没合入）")
        except Exception:
            rep.add("8 心率", WARN, "心率关抛异常", error=traceback.format_exc())
    else:
        rep.add("8 心率", SKIP, "本次跳过")

    if out_dir:
        try:
            txt = os.path.join(out_dir, "checkup_report.txt")
            with open(txt, "w", encoding="utf-8") as fh:
                fh.write(rep.print_table() + "\n")
            if raw_snap is not None:
                fh.write("\n第 4 关证据： " + repr(raw_snap.extra) + "\n")
            print(f"[checkup] 报告已写 {txt}", flush=True)
        except Exception:
            pass

    rep.print_table()
    return rep


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="聆心 · 成员B 上板自检（8 关）")
    ap.add_argument("bit", nargs="?", default=None, help=".bit 路径；省略则尝试降级")
    ap.add_argument("--out", default=None, help="产物目录")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--sim-rate", type=float, default=None, help="离线模拟采样率（无板时用）")
    a = ap.parse_args(argv)
    sim = None
    if a.sim_rate:
        from .fake_afe import FakeAfe
        sim = FakeAfe(rate=a.sim_rate)
    rep = run_checkup(a.bit, sim=sim, out_dir=a.out, seconds=a.seconds)
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
