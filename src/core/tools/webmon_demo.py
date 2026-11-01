"""webmon_demo.py —— PC 侧网页监视演示（无板，用 FakeAfe 造实时数据流）。

用法：python tools/webmon_demo.py [--fs 125] [--port 8765] [--plugins a,b] [--seconds 60]。
浏览器打开输出里的 http://127.0.0.1:<port>/ 看波形/心率/HRV/节律/间期/质量，可点按钮控制。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, ".."))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ecgb.acq_loop import AcquisitionLoop            # noqa: E402
from ecgb.fake_afe import make_fake_pynq_overlay      # noqa: E402
from ecgb.plugins import PluginHost, available        # noqa: E402
from ecgb.ps_afe import PsAfe                         # noqa: E402

DEFAULT_PLUGINS = "quality,hr_alarm,recorder,quick30,hrv,arrhythmia,intervals,voice,webmon"


def main() -> int:
    ap = argparse.ArgumentParser(description="网页监视演示（FakeAfe 数据源）")
    ap.add_argument("--fs", type=float, default=31.25,
                    help="有效采样率（默认 31.25=板上口径。⚠ Windows 定时器粒度约 15.6 ms，"
                         "软件轮询上限 ~60 Hz；调高 --fs 必须确认 est_fs_sw≈fs，否则会掉样）")
    ap.add_argument("--port", type=int, default=8765, help="网页端口")
    ap.add_argument("--seconds", type=float, default=0.0, help="运行时长（0=一直跑，Ctrl+C 结束）")
    ap.add_argument("--plugins", type=str, default=DEFAULT_PLUGINS,
                    help=f"启用的插件（all=全部；可用：{','.join(available())}）")
    ap.add_argument("--out", type=str, default=os.path.join(os.path.abspath(os.path.join(HERE, "..")), "records"))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--hr", type=float, default=1.2, help="模拟心率（Hz，1.2≈72 bpm）")
    args = ap.parse_args()

    for s in (sys.stdout, sys.stderr):
        try:
            if not s.isatty():
                s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    os.makedirs(args.out, exist_ok=True)
    ol = make_fake_pynq_overlay(rate=args.fs, seed=args.seed, jitter=0.015, hr_hz=args.hr,
                                noise=25.0, ecg_amp=1200.0)
    drv = PsAfe(sim=ol, verbose=True)                 # 显式 sim：绝不触碰 pynq
    fs = drv.measure_rate(window=1.0)
    print(f"[demo] 演示采样率 fs = {fs:.2f} S/s（板上请用 measure_rate 实测值；"
          f"模拟心率 {args.hr:g} Hz = {args.hr*60:.0f} bpm）")

    loop = AcquisitionLoop(drv, fs=fs, win_sec=6.0, source="filt")
    host = PluginHost.from_spec(args.plugins,
                                fs=fs, out_dir=args.out, loop=loop, verbose=True,
                                params={"webmon": {"port": args.port, "max_points": 3000}})
    print(f"[demo] 插件：{args.plugins}")
    print(f"[demo] 打开浏览器访问 http://127.0.0.1:{args.port}/  （Ctrl+C 结束）")

    last = [0.0]
    warm = [0.0]

    def on_snap(s):
        host.dispatch(s)
        # 采集完整性守门：实收速率明显低于标称 → 掉样会让 HR/时间轴一起错，必须吼出来
        if warm[0] == 0.0:
            warm[0] = s.t
        elif s.t - warm[0] > 3.0:
            est = float(s.extra.get("est_fs_sw") or 0.0)
            if warm[0] > 0 and est < 0.9 * fs:
                print(f"[demo] ⚠ 实收 fs_sw={est:.1f} < 标称 {fs:.1f} S/s：本机轮询跟不上。"
                      f"请调低 --fs（建议 ≤ 30，Windows sleep 粒度约 15.6 ms）", flush=True)
            warm[0] = float("inf")
        now = time.monotonic()
        if now - last[0] >= 5.0:
            last[0] = now
            print(f"{s.n_total:7d} 点 | fs_sw={s.extra.get('est_fs_sw', 0):6.2f} | "
                  f"丢样={s.dropped:+4d} | HR={s.hr} ({'可信' if s.hr_ok else '--'})",
                  flush=True)

    seconds = args.seconds if args.seconds > 0 else 1e9
    try:
        loop.run(seconds=seconds, on_snapshot=on_snap, snapshot_dt=0.05)
    except KeyboardInterrupt:
        print("\n[demo] 收到 Ctrl+C，收尾……")
    finally:
        try:
            loop.dump_csv(os.path.join(args.out, "wave_demo.csv"))
        except Exception as exc:
            print(f"[demo] dump_csv 跳过：{exc!r}")
        host.close()
        drv.stop()
        print("[demo] 收尾完成：", {p.name: p.stats()["n_snapshots"] for p in host.plugins})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
