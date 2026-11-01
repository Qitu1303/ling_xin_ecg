"""test_plugins_offline.py —— 周边功能离线验收（无板、无 pytest 依赖）。

运行：python tests/test_plugins_offline.py；18 组用例覆盖 HrResult 扩展、宿主隔离、各插件
（quality/hr_alarm/recorder/hrv/arrhythmia/intervals/quick30/webmon/voice）与基线兼容性。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SRC = ROOT
if SRC not in sys.path:
    sys.path.insert(0, SRC)

for _stream in (sys.stdout, sys.stderr):
    try:
        if not _stream.isatty() and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

import numpy as np  # noqa: E402

from ecgb import plugins as P                                      # noqa: E402
from ecgb.acq_loop import AcquisitionLoop                          # noqa: E402
from ecgb.ecg_hr import HrDetector                                 # noqa: E402
from ecgb.plugins import PluginBase, PluginHost                    # noqa: E402
from ecgb.sim_ecg import make_ecg, truth_hr_bpm                    # noqa: E402
from ecgb.webmon import Hub, WebMonitorServer                      # noqa: E402

FAILS = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ok   {msg}")
    else:
        print(f"  FAIL {msg}")
        FAILS.append(msg)


class Snap:
    """最小快照替身（字段与 acq_loop.Snapshot 同名）。"""

    def __init__(self, wave=None, fs=200.0, hr=None, hr_ok=False, dropped=0,
                 n_total=0, n_hw=0, t=0.0, extra=None):
        self.wave = wave
        self.fs = fs
        self.hr = hr
        self.hr_ok = hr_ok
        self.dropped = dropped
        self.n_total = n_total
        self.n_hw = n_hw
        self.t = t
        self.extra = extra or {}


def _host(spec: str, out_dir: str, extra=None):
    return PluginHost.from_spec(spec, fs=200.0, out_dir=out_dir, loop=None,
                                verbose=False, extra=extra)


# ---------------------------------------------------------------------------
def test_hrresult_extension():
    print("[1] HrResult 只增不改扩展")
    fs = 31.25
    wave, r_true = make_ecg(fs, 20.0, 72.0, noise=60.0, drift=600.0, seed=1, return_truth=True)
    det = HrDetector(fs=fs, buffer_sec=30.0)
    res = det.process(wave)
    check(res.ok is True, f"hr_ok 仍为 True（hr={res.hr_bpm}）")
    check(abs(float(res.hr_bpm) - float(truth_hr_bpm(r_true, fs))) < 5.0, "心率与真值一致（±5 bpm）")
    check(len(res.beat_t_s) >= 5, f"新增 beat_t_s 非空（{len(res.beat_t_s)} 拍）")
    check(len(res.beat_idx) == len(res.beat_t_s), "beat_idx 与 beat_t_s 等长")
    check(len(res.rr_ms) >= 4, f"新增 rr_ms 非空（{len(res.rr_ms)} 段）")
    check(isinstance(res.reason, str) and isinstance(res.quality, float), "原有五字段类型不变")


def test_host_isolation():
    print("[2] 宿主异常隔离 + 自动停用")
    class Boom(PluginBase):
        name = "boom"
        priority = 1

        def on_snapshot(self, snap):
            raise RuntimeError("故意的")

    class Counter(PluginBase):
        name = "counter"
        priority = 5

        def __init__(self, **kw):
            super().__init__(**kw)
            self.n = 0

        def on_snapshot(self, snap):
            self.n += 1

    with tempfile.TemporaryDirectory() as td:
        host = _host("", td, extra=[Boom(), Counter()])
        for i in range(6):
            host.dispatch(Snap(t=float(i)))
        st = {p["name"]: p for p in host.status()["plugins"]}
        check(st["boom"]["enabled"] is False, "连续异常插件已自动停用")
        check(st["boom"]["n_errors"] >= 3, f"异常计数 {st['boom']['n_errors']}")
        check(st["counter"]["n_snapshots"] == 6, "其余插件不受影响（6 帧全收）")
        host.close()


def test_quality():
    print("[3] quality 正常/饱和/恢复")
    fs = 200.0
    wave_ok = make_ecg(fs, 2.0, 72.0, noise=25.0, seed=2)
    wave_sat = np.full(int(fs * 2), 65535, dtype=np.uint16)
    with tempfile.TemporaryDirectory() as td:
        host = _host("quality", td)
        t = 0.0
        for _ in range(20):
            host.dispatch(Snap(wave=wave_ok, fs=fs, n_total=len(wave_ok), t=t))
            t += 0.1
        q1 = host.latest("quality")
        check(q1 and q1["electrode"] == "良好", f"正常信号 → 良好（SQI {q1 and q1['sqi']}）")
        for _ in range(30):
            host.dispatch(Snap(wave=wave_sat, fs=fs, n_total=len(wave_sat), t=t, dropped=0))
            t += 0.1
        q2 = host.latest("quality")
        check(q2 and q2["electrode"] == "脱落", f"饱和贴轨 → 脱落（sat={q2 and q2['sat_ratio']}）")
        evs = [e for e in host.events() if e["code"] == "lead_off"]
        check(len(evs) >= 1, "上报 lead_off 事件")
        for _ in range(40):
            host.dispatch(Snap(wave=wave_ok, fs=fs, n_total=len(wave_ok), t=t))
            t += 0.1
        q3 = host.latest("quality")
        check(q3 and q3["electrode"] == "良好", "恢复后回到良好")
        host.close()


def test_hr_alarm():
    print("[4] hr_alarm 分级与恢复")
    with tempfile.TemporaryDirectory() as td:
        host = _host("hr_alarm", td)
        t = 0.0

        def feed(hr, ok, n, dt=0.2):
            nonlocal t
            for _ in range(n):
                host.dispatch(Snap(hr=hr, hr_ok=ok, t=t))
                t += dt

        feed(70, True, 15)                        # 3 s 正常
        check(host.latest("alarm")["state"] == "正常", "初始正常")
        feed(130, True, 20)                       # 4 s 过速
        check(host.latest("alarm")["state"] == "心动过速", "持续过速 → 心动过速")
        check(any(e["code"] == "tachy" for e in host.events()), "上报 tachy 事件")
        feed(160, True, 6)
        check(host.latest("alarm")["state"] == "心率危急", "极端区 → 心率危急（high）")
        feed(70, True, 20)
        st = host.latest("alarm")
        check(st["state"] == "正常", "迟滞后恢复正常")
        check(any(e["code"] == "hr_recovered" for e in host.events()), "上报恢复事件")
        host.close()


def test_recorder():
    print("[5] recorder CSV 分段 + 摘要")
    with tempfile.TemporaryDirectory() as td:
        host = _host("recorder", td, extra=None)
        host.plugins[0].params.update({"seg_seconds": 60, "queue_size": 128})
        host.plugins[0].on_command("start")
        fs = 200.0
        total = 0
        for k in range(10):
            wave = make_ecg(fs, 0.5, 72.0, seed=3)          # 100 点/帧
            total += len(wave)
            host.dispatch(Snap(wave=wave, fs=fs, n_total=total, t=float(k) * 0.5))
        host.plugins[0].on_command("stop")
        rec = host.latest("recorder")
        sess = rec["session_dir"]
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not os.path.isfile(
                os.path.join(sess, "summary.json")):
            time.sleep(0.05)                             # 收尾是异步的（不阻塞采集线程）
        check(not host.plugins[0]._finalizing, "收尾线程已结束")
        check(os.path.isfile(os.path.join(sess, "part01.csv")), "part01.csv 已生成")
        with open(os.path.join(sess, "part01.csv"), encoding="utf-8") as fh:
            lines = fh.read().strip().splitlines()
        check(lines[0] == "n,code,volt,t_s", "表头正确")
        rows = len(lines) - 1
        # 首帧只建立游标（新会话不回灌整窗），其余帧各写入 100 个新样本
        check(0.85 * total <= rows <= total, f"行数≈新增样本数（{rows}/{total}）")
        ns = [int(l.split(",")[0]) for l in lines[1:]]
        check(ns and ns[0] == 100, f"首行从游标处开始（n={ns[0] if ns else '--'}）")
        check(all(ns[i] + 1 == ns[i + 1] for i in range(len(ns) - 1)),
              "样本号严格递增且无重复（防整窗重写）")
        with open(os.path.join(sess, "summary.json"), encoding="utf-8") as fh:
            summ = json.load(fh)
        check(summ["n_written"] == rows, f"摘要样本数与文件一致（{summ['n_written']}）")
        check(isinstance(summ.get("note"), str) and "演示" in summ["note"], "摘要含免责声明")
        host.close()


def test_hrv():
    print("[6] hrv 时域 + 频域（含窗口不足降级）")
    with tempfile.TemporaryDirectory() as td:
        host = _host("hrv", td)
        rng = np.random.default_rng(7)
        fs = 32.0
        prev_abs = 0
        t = 0.0
        for k in range(70):                                # 70 拍 ≈ 70 s
            rr = 1.0 + float(rng.normal(0, 0.03))
            cur_abs = prev_abs + int(round(rr * fs))
            beats = {"idx_abs": (prev_abs, cur_abs), "t_s": (0.0, float(cur_abs - prev_abs) / fs),
                     "rr_ms": (float(cur_abs - prev_abs) / fs * 1000.0,),
                     "buf_offset": prev_abs, "n_beats": 2, "hr_quality": 0.9}
            host.dispatch(Snap(fs=fs, n_total=cur_abs, t=t, extra={"beats": beats}))
            prev_abs, t = cur_abs, t + rr
        hrv = host.latest("hrv")
        w60 = hrv["windows"]["60s"]
        w30 = hrv["windows"]["30s"]
        check(w60["ready"] and w60["sdnn_ms"] is not None, f"60 s 时域 ready（SDNN {w60.get('sdnn_ms')} ms）")
        check(w60.get("freq_ready") is True, "60 s 频域 ready（≥50 s）")
        check(w60.get("lf_hf") is not None, f"LF/HF 已算出（{w60.get('lf_hf')}）")
        check(w30.get("freq_ready") is False, "30 s 频域按预期降级（窗口不足）")
        check(5.0 < w60["sdnn_ms"] < 80.0, "SDNN 在注入抖动（σ=30 ms）合理范围")
        host.close()


def test_arrhythmia():
    print("[7] arrhythmia 长间歇 / 心动过速")
    with tempfile.TemporaryDirectory() as td:
        host = _host("arrhythmia", td)
        fs = 32.0
        prev_abs, t = 0, 0.0

        def beat(rr_s):
            nonlocal prev_abs, t
            cur = prev_abs + int(round(rr_s * fs))
            extra = {"beats": {"idx_abs": (prev_abs, cur), "t_s": (0.0, rr_s),
                               "rr_ms": (rr_s * 1000.0,), "buf_offset": prev_abs, "n_beats": 2}}
            host.dispatch(Snap(fs=fs, n_total=cur, t=t, extra=extra))
            prev_abs, t = cur, t + rr_s

        for _ in range(6):
            beat(1.0)
        beat(2.5)
        for _ in range(3):
            beat(1.0)
        check("pause" in host.latest("arrhythmia")["flags"], "识别长间歇")
        for _ in range(10):
            beat(0.4)                                       # 150 bpm
        arr = host.latest("arrhythmia")
        check("tachy" in arr["flags"], "识别心动过速（150 bpm → high）")
        check(any(e["code"] == "pause" for e in host.events()), "上报 pause 事件")
        host.close()


def test_intervals():
    print("[8] intervals 模板平均测量")
    fs = 31.25
    wave, _ = make_ecg(fs, 20.0, 72.0, noise=40.0, drift=500.0, seed=5, return_truth=True)
    det = HrDetector(fs=fs, buffer_sec=30.0)
    res = det.process(wave)
    with tempfile.TemporaryDirectory() as td:
        host = _host("intervals", td)
        extra = {"beats": {"idx_abs": tuple(int(res.buf_offset) + int(v) for v in res.beat_idx),
                           "t_s": res.beat_t_s, "rr_ms": res.rr_ms,
                           "buf_offset": int(res.buf_offset), "n_beats": int(res.n_beats)}}
        for k in range(6):
            host.dispatch(Snap(wave=wave.astype(np.uint16), fs=fs, n_total=len(wave),
                               t=3.0 * k, extra=dict(extra)))
        itv = host.latest("intervals")
        check(itv.get("ready") is True, f"测量就绪（用 {itv.get('n_beats')} 拍平均）")
        check(itv.get("qrs_ms") is not None and 40 <= itv["qrs_ms"] <= 220,
              f"QRS 宽度合理（{itv.get('qrs_ms')} ms）")
        check(itv.get("precision_ms") == round(1000.0 / fs, 1), "产出量化精度声明")
        print(f"       PR={itv.get('pr_ms')} QRS={itv.get('qrs_ms')} "
              f"QT={itv.get('qt_ms')} QTc={itv.get('qtc_ms')} note={itv.get('note', '')[:18]}…")
        host.close()


def test_quick30():
    print("[9] quick30 快速测量报告")
    with tempfile.TemporaryDirectory() as td:
        host = _host("quick30", td)
        host.plugins[0].params["duration"] = 1.0
        host.submit("quick30", "start")
        t = 0.0
        for _ in range(24):                                  # 1.2 s @20 Hz
            host.dispatch(Snap(hr=72, hr_ok=True, t=t, extra={"est_fs_sw": 200.0}))
            t += 0.05
        rep = host.latest("quick30")["last_report"]
        check(rep is not None, "产出报告对象")
        check(abs(rep["hr"]["mean"] - 72.0) < 0.5, f"HR 均值 {rep['hr']['mean']}")
        deadline = time.monotonic() + 5.0
        files = []
        while time.monotonic() < deadline:               # 落盘是异步的（不阻塞采集）
            files = [f for f in os.listdir(td) if f.startswith("quick30_")]
            if files:
                break
            time.sleep(0.05)
        check(bool(files), "报告 JSON 已落盘（异步写出）")
        check("非诊断" in rep["verdict"] or "演示级" in rep["note"], "报告含免责声明")
        host.close()


def test_webmon():
    print("[10] webmon HTTP/SSE/命令")
    submitted = []

    def fake_submit(target, action, arg=None):
        submitted.append((target, action, arg))
        return True

    hub = Hub(submit=fake_submit, status_provider=lambda: {"ok": 1},
              events_provider=lambda seq: [], meta={"version": "test"})
    srv = WebMonitorServer(hub, host="127.0.0.1", port=0, verbose=False)
    url = srv.start()
    try:
        with urllib.request.urlopen(url + "healthz", timeout=5) as r:
            check(r.status == 200, "GET /healthz 200")
        with urllib.request.urlopen(url + "api/status", timeout=5) as r:
            body = json.load(r)
        check("server" in body and "meta" in body, "/api/status 返回服务与元信息")
        req = urllib.request.Request(url + "api/command",
                                     data=json.dumps({"target": "recorder", "action": "start"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.load(r)
        check(body.get("ok") is True and submitted == [("recorder", "start", None)],
              "POST /api/command 转发到宿主")
        t = threading.Thread(target=lambda: hub.publish({"fs": 200.0, "wave": [1, 2, 3]}),
                             daemon=True)
        t.start()
        raw = b""
        try:
            with urllib.request.urlopen(url + "events", timeout=5) as r:
                for _ in range(120):                     # 增量读，凑齐两个事件即停
                    raw += r.read(16)
                    if b"event: hello" in raw and b"event: snapshot" in raw:
                        break
        except TimeoutError:
            pass
        chunk = raw.decode("utf-8", "ignore")
        check("event: hello" in chunk, "SSE 首包 hello")
        check("event: snapshot" in chunk, "SSE 收到 snapshot 帧")
    finally:
        srv.stop()


def test_voice():
    print("[11] voice WAV 合成")
    with tempfile.TemporaryDirectory() as td:
        host = _host("voice", td)
        v = host.plugins[0]
        v.ctx = host.ctx.child("voice")
        data = v._pattern_wav("high")
        check(data[:4] == b"RIFF" and data[8:12] == b"WAVE", "WAV 头合法")
        check(len(data) > 44 + 1000, f"含音频数据（{len(data)} 字节）")
        host.close()


def test_backcompat_extra():
    print("[12] 基线兼容：extra 键集合保持不变")
    class StubAfe:
        def __init__(self):
            self.cnt = 1000

        def read_cnt(self):
            self.cnt += 5
            return self.cnt

        def read_raw(self):
            return (32768, True)

        def write(self, offset, value):
            pass

    loop = AcquisitionLoop(StubAfe(), fs=200.0, win_sec=2.0, verbose=False)
    loop.tick()
    snap = loop.snapshot()
    baseline = {"duration", "n_samples", "est_fs_sw", "fs_calibrated", "poll_hz",
                "dup_ratio", "missed_poll_ratio", "out_of_range_ratio",
                "interval_p50", "interval_p95", "last_error"}
    check(baseline <= set(snap.extra.keys()), "原有 11 个键全部保留")
    check(isinstance(snap.wave, np.ndarray) and snap.wave.dtype == np.uint16, "wave 契约不变")
    print(f"       extra 新增键：{sorted(set(snap.extra) - baseline)}")


def test_up_shift_convention():
    print("[13] _up_shift 亚样本对齐约定（审计补测）")
    from ecgb.plugins.intervals import _up_shift
    seg = np.zeros(100, dtype=np.float64)
    seg[50] = 1000.0
    # 约定：out[m] = seg(m/factor + shift) —— 即"输出第 m 点取源上 m/factor+shift 处"
    out = _up_shift(seg, 8, 0.5)
    check(abs(out[400] - 500.0) < 1e-6, "输出 400 对应源位置 50.5（半幅插值，验证约定）")
    check(abs(out[396] - 1000.0) < 1e-6, "输出 396 对应源位置 50（整点搬移）")
    # intervals 的用法：段内 R 的亚样本位置在 npre+frac，传 +frac 才能把它搬到 npre。
    # 用"阶梯过渡带"做地标（不受平滑峰插值偏差影响）：地标中心 50.5，
    # +frac 应把 0.5 交点搬到 ≈400.8，旧写法（-frac）则会推到 ≈407.2。
    step = (np.arange(100) >= 51).astype(np.float64)

    def cross_idx(out):
        idx = np.where(out >= 0.5)[0]
        return int(idx[0]) if idx.size else -1

    cg = cross_idx(_up_shift(step, 8, 0.4))
    cb = cross_idx(_up_shift(step, 8, -0.4))
    check(abs(cg - 401) <= 2, f"+frac 时 0.5 交点 {cg}（期望 ≈401）")
    check(abs(cb - cg) >= 5, f"-frac（旧写法）偏移 {cb - cg:+d} 个上采样样本（≥5 即说明修正有效）")


def test_beats_anchor():
    print("[14] 节拍绝对样本号锚点（审计补测：防 buf_offset 恒 0 的坑）")
    fs = 31.25
    wave, _ = make_ecg(fs, 30.0, 72.0, noise=30.0, seed=11, return_truth=True)

    class SlideAfe:
        """回放整段波形，read_cnt 与 read_raw 配套（new_flag 恒 1）。"""

        def __init__(self, data):
            self.data = data
            self.i = 0

        def read_cnt(self):
            return self.i

        def read_raw(self):
            v = int(self.data[min(self.i, self.data.size - 1)])
            self.i += 1
            return v, True

        def write(self, offset, value):
            pass

    loop = AcquisitionLoop(SlideAfe(wave), fs=fs, win_sec=8.0, verbose=False)
    for _ in range(int(wave.size)):
        loop.tick()
    snap = loop.snapshot()
    b = snap.extra.get("beats") or {}
    idx = list(b.get("idx_abs") or ())
    w_start = snap.n_total - len(snap.wave)
    check(len(idx) >= 3, f"拿到拍序列（{len(idx)} 拍）")
    check(all(w_start <= v <= snap.n_total for v in idx), "拍绝对号落在当前窗口内")
    check(all(idx[i] < idx[i + 1] for i in range(len(idx) - 1)), "拍绝对号严格递增")
    if idx:
        check(idx[-1] >= snap.n_total - int(fs * 1.5), "最新拍靠近窗口末端（锚点未漂移）")


def test_cli_and_params():
    print("[15] 命令行解析与参数文件（审计补测：零侵入承诺）")
    from ecgb.plugins import parse_plugin_cli, load_params_file
    rest, spec, pf = parse_plugin_cli(["bit.bin", ".", "120"])
    check(rest == ["bit.bin", ".", "120"] and spec == "" and pf is None,
          "缺省时三个位置参数原样保留（零侵入）")
    rest, spec, pf = parse_plugin_cli(["--plugins", "a,b", "bit.bin", ".", "9", "--plugins-file", "p.json"])
    check(rest == ["bit.bin", ".", "9"] and spec == "a,b" and pf == "p.json",
          "两种可选参数可混排且不破坏位置参数")
    try:
        P.PluginHost.from_spec("no_such_plugin", fs=1.0, out_dir=".", verbose=False)
        check(False, "未知名应报错")
    except ValueError:
        check(True, "未知插件名明确报错")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "p.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"webmon": {"port": 9}}, fh)
        params = load_params_file(path)
        check(params.get("webmon", {}).get("port") == 9, "参数文件读取正确")


def test_recorder_rotation():
    print("[16] recorder 分段轮转（审计补测）")
    with tempfile.TemporaryDirectory() as td:
        host = _host("recorder", td)
        rec = host.plugins[0]
        rec.params["seg_seconds"] = 0.05                 # 10 样本/段（fs=200）
        rec.on_command("start")
        fs, total = 200.0, 0
        for k in range(12):
            wave = make_ecg(fs, 0.25, 72.0, seed=4)      # 50 样本/帧
            total += len(wave)
            host.dispatch(Snap(wave=wave, fs=fs, n_total=total, t=float(k) * 0.25))
        rec.on_command("stop")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and rec._finalizing:
            time.sleep(0.05)                             # 等异步收尾结束
        sess = host.latest("recorder")["session_dir"]
        parts = sorted(f for f in os.listdir(sess) if f.startswith("part"))
        check(len(parts) >= 3, f"按 0.05 s 分段轮转（{len(parts)} 段）")
        check(os.path.isfile(os.path.join(sess, "summary.json")), "异步收尾仍写出摘要")
        host.close()


def test_sse_shutdown():
    print("[17] SSE 收尾回收（审计补测）")
    hub = Hub(submit=lambda t, a, arg=None: True, status_provider=lambda: {},
              events_provider=lambda seq: [], meta={})
    srv = WebMonitorServer(hub, host="127.0.0.1", port=0, verbose=False)
    url = srv.start()
    t = threading.Thread(target=lambda: hub.publish({"fs": 1.0}), daemon=True)
    t.start()
    holder = {}

    def reader():
        # 读大块 → 连接保持打开（小读会立刻返回并关闭连接，客户端随即被回收）
        try:
            with urllib.request.urlopen(url + "events", timeout=10) as r:
                data = r.read(8192)
                holder["chunk"] = data[:240]              # 只留开头，便于诊断
        except Exception as exc:                          # 连接被服务端关闭属预期
            holder["err"] = repr(exc)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    time.sleep(0.2)                                      # 给建连一点时间（Windows 调度较慢）
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and hub.status()["clients"] != 1:
        time.sleep(0.05)
    ok_reg = hub.status()["clients"] == 1
    check(ok_reg, "SSE 客户端已登记"
          + ("" if ok_reg else f"（诊断：clients={hub.status()['clients']} "
                              f"holder={ {k: (len(v) if isinstance(v, bytes) else v) for k, v in holder.items()} }）"))
    srv.stop()
    th.join(timeout=3.0)
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline and hub.status()["clients"] != 0:
        time.sleep(0.05)                                 # 线程退出需要一点时间
    check(hub.status()["clients"] == 0, "stop() 后客户端计数归零（线程已回收）")


def test_board_like_lowrate_session():
    """板端口径长会话：fs=19.53（板端历史实测）、全插件、90 s 虚拟时长。

    核对两件事：① 正确性 —— HRV 频域 / 间期测量 / 记录条数在低采样率下仍成立；
    ② 成本 —— 各插件单帧耗时是否在宿主预算内（PC 数据只作下界参考）。
    拍序列由 sim_ecg 真值给出，故衡量的是**插件层**的成本与正确性。
    """
    print("[18] 板端口径长会话（fs=19.53 / 全插件 / 90 s 虚拟时长）")
    import bisect
    fs, win_sec, total_s = 19.53, 8.0, 90.0
    wave, r_idx = make_ecg(fs, total_s, 72.0, noise=70.0, drift=900.0, mains=90.0,
                           seed=21, return_truth=True)
    win = int(round(fs * win_sec))
    beats_abs = [int(v) for v in r_idx]
    with tempfile.TemporaryDirectory() as td:
        host = PluginHost.from_spec(
            "quality,hr_alarm,recorder,quick30,hrv,arrhythmia,intervals,voice,webmon",
            fs=fs, out_dir=td, loop=None, verbose=False,
            params={"webmon": {"port": 0}, "recorder": {"seg_seconds": 30.0}})
        dt = 0.05
        for k in range(int(total_s / dt)):
            n_total = min(int(round((k + 1) * dt * fs)), int(wave.size))
            if n_total < win:
                continue
            start = n_total - win
            seg = wave[start:n_total]
            b = beats_abs[bisect.bisect_left(beats_abs, start):bisect.bisect_left(beats_abs, n_total)]
            rr = [(b[i + 1] - b[i]) / fs * 1000.0 for i in range(len(b) - 1)]
            med = sorted(rr)[len(rr) // 2] if rr else 0.0
            hr = int(round(60000.0 / med)) if med > 0 else None
            extra = {"est_fs_sw": fs * 0.99, "dup_ratio": 0.0, "missed_poll_ratio": 0.4,
                     "last_error": None,
                     "beats": {"idx_abs": tuple(b),
                               "t_s": tuple((v - start) / fs for v in b),
                               "rr_ms": tuple(rr), "buf_offset": start,
                               "n_beats": len(b), "hr_quality": 0.9}}
            host.dispatch(Snap(wave=seg, fs=fs, hr=hr, hr_ok=bool(hr), dropped=0,
                               n_total=n_total, n_hw=n_total, t=k * dt, extra=extra))
        host.close()
        st = {p["name"]: p for p in host.status()["plugins"]}
        worst = max(v["max_ms"] for v in st.values())
        slow = sum(v["n_slow"] for v in st.values())
        check(worst < 15.0, f"单帧最坏耗时 {worst:.2f} ms（预算 15 ms）")
        check(slow == 0, f"无超预算帧（n_slow={slow}）")
        hrv = host.latest("hrv") or {}
        w60 = (hrv.get("windows") or {}).get("60s", {})
        check(bool(w60.get("ready")) and bool(w60.get("freq_ready")),
              "19.53 S/s 下 HRV 60 s 出齐时域+频域")
        itv = host.latest("intervals") or {}
        # 19.53 S/s（量化 51 ms）下必须**如实降级**：QRS/QT 不报，理由里写明采样率
        check(bool(itv.get("ready")) and itv.get("qrs_ms") is None
              and "采样率" in str(itv.get("reason", "")),
              f"低采样率如实降级（{itv.get('reason')}）")
        check(itv.get("precision_ms") == round(1000.0 / fs, 1),
              f"量化精度随实测 fs 声明（±{1000.0 / fs:.1f} ms）")
        rec = host.latest("recorder") or {}
        n_src = int(wave.size)
        n_rec = int(rec.get("n_written", 0))
        check(0.85 * n_src <= n_rec <= 1.05 * n_src,
              f"记录条数≈源样本数（{n_rec}/{n_src}，滑动窗不重复写）")
        print("       成本表（avg/max ms·帧⁻¹）：" + "  ".join(
            f"{n}={v['avg_ms']:.3f}/{v['max_ms']:.2f}" for n, v in st.items()))


def main() -> int:
    print("=" * 68)
    print("周边功能离线验收（无板）")
    print("=" * 68)
    for fn in (test_hrresult_extension, test_host_isolation, test_quality, test_hr_alarm,
               test_recorder, test_hrv, test_arrhythmia, test_intervals, test_quick30,
               test_webmon, test_voice, test_backcompat_extra, test_up_shift_convention,
               test_beats_anchor, test_cli_and_params, test_recorder_rotation,
               test_sse_shutdown, test_board_like_lowrate_session):
        fn()
    print("=" * 68)
    if FAILS:
        print(f"结果：{len(FAILS)} 项失败")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("结果：全部通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
