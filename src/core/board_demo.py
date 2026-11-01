"""board_demo.py —— 板端主循环演示：自检 → 实测采样率 → 主循环（波形/心率/喂显示）。

主循环前 5 s 为预热窗口：显示与插件负载就绪后才用软件投递率标定 fs，此前 HR 显示 "--"。
仅用于演示与验证，不作医疗诊断。
"""
import dataclasses
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from ecgb.acq_loop import AcquisitionLoop
from ecgb.checkup import run_checkup
from ecgb.display_link import DisplayLink, NullDisplay
from ecgb.plugins import PluginHost, load_params_file, parse_plugin_cli, summarize
from ecgb.ps_afe import PsAfe
from ecgb.sim_afe import SimAfe, parse_sim_cli, print_cases, run_selftest

# —— 参数解析：先摘出 --plugins / --plugins-file，再保持原 3 个位置参数语义 ——
_argv, PLUGINS, PLUGINS_FILE = parse_plugin_cli(sys.argv[1:])
    # —— 再摘 --source sim / --sim-*（不给 --source sim 时 SIM 为空 ⇒ 零侵入）——
_argv, SIM = parse_sim_cli(_argv)
    # 快照间隔可调：默认 0.05；负载吃紧时可传 0.1（显示推链与插件派发都按快照同步执行）。
SNAPSHOT_DT = 0.05
if '--snapshot-dt' in _argv:
    _i = _argv.index('--snapshot-dt')
    if _i + 1 < len(_argv):
        try:
            SNAPSHOT_DT = max(0.01, float(_argv[_i + 1]))
        except ValueError:
            raise SystemExit(f'--snapshot-dt 取值不合法：{_argv[_i + 1]!r}')
        del _argv[_i:_i + 2]
    else:
        raise SystemExit('--snapshot-dt 缺少取值')
BIT = _argv[0] if len(_argv) > 0 else 'design_1_wrapper.bit'
OUT = _argv[1] if len(_argv) > 1 else HERE
RUN_SECONDS = float(_argv[2]) if len(_argv) > 2 else 120.0


def _utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            if not s.isatty():
                s.reconfigure(encoding='utf-8')
        except Exception:
            pass


def main() -> int:
    _utf8()
    # ---- 模拟源的分支控制位（不给 --source sim 时 SIM 为空，下面整段都不进）----
    if SIM.get('_list'):
        print_cases()
        return 0
    if SIM.get('_selftest'):
        return run_selftest()

    if SIM:
        # 模拟源：**跳过 8 关自检**（那是查硬件的，sim 无 IP 可读；checkup 用 afe.ip.read）。
        # 横幅由 SimAfe 自己打（ps_afe L40-47 的纪律：合成数据绝不能混成上板结论）。
        sim_opts = {k: v for k, v in SIM.items() if not k.startswith('_')}
        print('=' * 68)
        print('=== 第 1 步：跳过 8 关自检（--source sim：数据是合成的，无硬件可查）===')
        afe = SimAfe(**sim_opts)
        print(afe.summary())
        # sim+HDMI：给了真实 bit 路径就为推屏预载工程 overlay（sim 采集是纯 PS 链，bit 只服务显示）。
        # **必须在 start() 之前载**：start() 起算投递时标，之后载 bit 的数秒会积成投递欠账，
        # 注入窗随之整体前移掉进预热区。'x' 之类占位路径维持不推屏，webmon 通道照常。
        if BIT and os.path.isfile(BIT):
            try:
                from pynq import Overlay    # 与 ps_afe._try_pynq 同款（BaseOverlay 不在 pynq 顶层）
                afe._ol = Overlay(BIT)      # 挂到 _ol：复用下方 M4 显示判定，EcgDisplay 正常建
                print(f'[board_demo] sim+HDMI：工程 overlay 已载（{BIT}），本场推 HDMI')
            except Exception as exc:
                print(f'[board_demo] sim+HDMI：overlay 载入失败（{exc!r}）→ 本次不推屏')
        else:
            print('[board_demo] sim 模式：bit 为占位路径 → 本次不推屏（webmon 照常）')
        afe.start()
        # sim 的投递率是锁死的，标定 1 s 就够（真板那 3 s 是为了等硬件口径稳定）
        fs_hw = afe.measure_rate(window=1.0)
        print(f'fs_hw = {fs_hw:.3f} S/s    （SimAfe 实测投递率，sim 锁死 ≈ --sim-fs）')
    else:
        print('=' * 68)
        print('=== 第 1 步：8 关自检 ===')
        rep = run_checkup(BIT, out_dir=OUT, seconds=10.0)
        if not rep.ok:
            print('自检不过：先解决上面最先 FAIL 的那一关，再看心率。')
            return 1

        print('=' * 68)
        print('=== 第 2 步：实测采样率 ===')
        afe = PsAfe(BIT)
        afe.start()    # ★ 新建即 start（en 复位坑：重下 overlay 后 en=0，不 start 则 sample_cnt 恒 0）
        fs_hw = afe.measure_rate(window=3.0)
        print(f'fs_hw = {fs_hw:.3f} S/s    （PL sample_cnt 硬件口径，仅作轮询粗值；理论 31.25）')

    # 预热必须在**最终负载下**做：显示/插件就绪前投递率偏高，用它标定会把 HR 放大。
    # 这里先用硬件口径占位（detector 构造需要合法 fs），真正标定在预热窗口结束时做。
    loop = AcquisitionLoop(afe, fs=fs_hw, win_sec=8.0)

    # 预热窗口长度：太短的会话按比例缩短，否则预热会把整场吃掉
    PREWARM_SEC = 5.0 if RUN_SECONDS > 10.0 else max(1.0, 0.3 * RUN_SECONDS)
    print('=' * 68)
    print(f'=== 第 3 步：主循环 {RUN_SECONDS:.0f} s（前 {PREWARM_SEC:.0f} s 为预热窗口，'
          '负载满、HR 显示 --）===')
    # M4：显示与采集共用同一个 overlay（工程 bit 已含 video/hdmi_out）。
    # 绝不能让 DisplayLink 走自动模式：它会新建 EcgDisplay() → BaseOverlay("base.bit")，
    # 把 PL 里的工程 bit 顶掉，采集链随即失联。
    disp = None
    ol = getattr(afe, '_ol', None)
    if ol is not None:
        try:
            from ecg_display import EcgDisplay          # 板上：C 的模块在 sys.path 上
            disp = EcgDisplay(ol)
            print('[board_demo] 显示：EcgDisplay(工程 overlay)')
        except Exception as exc:
            print(f'[board_demo] EcgDisplay 建立失败（{exc!r}）→ 本次不推屏')
    else:
        print('[board_demo] 未拿到工程 overlay（后端非 pynq）→ 本次不推屏')
    if disp is None:
        disp = NullDisplay()                            # 安全兜底：绝不误载 base.bit
    link = DisplayLink(disp=disp, fs=fs_hw, verbose=True)
    # loop 只建一次（上面），fs 由主循环预热窗口结束时的 calibrate 定稿，此处不得重建
    last = [0.0]

    # —— 插件宿主：仅当 --plugins / --plugins-file 给出时才构建（缺省零侵入）——
    host = None
    if PLUGINS or PLUGINS_FILE:
        params = load_params_file(PLUGINS_FILE)
        host = PluginHost.from_spec(PLUGINS, fs=fs_hw, out_dir=OUT, loop=loop,
                                    verbose=True, params=params)

    # ---- P2：预热窗口（在主循环内，显示与插件负载已全部就绪）----
    warm = {'done': False, 't0': 0.0}

    def _warm_snapshot(s):
        """预热期的快照 —— 波形照推，但 HR 与拍序列清空。

        fs 未按最终负载标定前算出的 HR 不可信，不能上屏，也不能给插件。
        """
        extra = {k: v for k, v in (s.extra or {}).items() if k != 'beats'}
        return dataclasses.replace(s, hr=None, hr_ok=False, extra=extra)

    def _finish_warmup():
        """预热窗口结束 —— 用该窗的软件投递率标定 fs。"""
        warm['done'] = True
        fs_sw = float(loop.stats().get('est_fs_sw', 0.0))
        if fs_sw >= 1.0 and 0.60 * fs_hw <= fs_sw <= 1.05 * fs_hw:
            loop.calibrate(rate=fs_sw)          # 同步更新 detector 的 fs 与缓冲容量
            print(f'fs = {loop.fs:.3f} S/s    （软件实测投递率，最终负载下测得 = HR 权威口径；'
                  f'硬件口径 {fs_hw:.3f}，投递缺口 {(1.0 - loop.fs / fs_hw) * 100:.1f}%）')
        else:
            print(f'fs = {loop.fs:.3f} S/s    （⚠ 软件口径 {fs_sw:.3f} 异常，保留硬件口径——'
                  '查 CPU 争抢/est_fs_sw）')
        # 插件上下文里的 fs 是构造期快照，标定后跟随最终值（recorder 的 t_s 列在 on_start
        # 已固化，不受影响；这里只是让仍在读 ctx.fs 的插件用上正确时基）
        if host is not None:
            try:
                host.ctx.fs = loop.fs
                for p in host.plugins:
                    if getattr(p, 'ctx', None) is not None:
                        p.ctx.fs = loop.fs
            except Exception:
                pass

    def on_snap(s):
        if not warm['done']:
            if (s.t - warm['t0']) < PREWARM_SEC:
                s = _warm_snapshot(s)           # 预热期：波形照推，HR 显示 "--"
            else:
                _finish_warmup()
                s = _warm_snapshot(s)           # 本帧仍按预热口径（下一帧起才有 HR）
        link.push(s.wave, s.hr, fs=s.fs)        # fs 每帧实测带入（重标定后立刻跟着变）
        if host is not None:                            # ★ 唯一侵入点
            host.dispatch(s)
        now = time.monotonic()
        if now - last[0] >= 5.0:
            last[0] = now
            st = s.extra
            flag = '可信' if s.hr_ok else '--'
            print(f'{s.n_total:7d} 点 | fs_used={st["fs_used"]:6.2f} | 丢样={s.dropped:+4d} | '
                  f'HR={s.hr} ({flag}) | 推屏={link.n_pushed}')

    warm['t0'] = time.monotonic()
    loop.run(seconds=RUN_SECONDS, on_snapshot=on_snap, snapshot_dt=SNAPSHOT_DT)
    link.close()
    if host is not None:
        host.close()
    afe.stop()

    print('--- 收尾统计 ---')
    for k, v in loop.stats().items():
        print(f'  {k} = {v}')
    print('  显示层：', link.stats())
    if host is not None:
        print('  插件层：', summarize(host))
        if SIM:
            # 仅模拟源模式打印事件流（演示时"注入什么出什么"要能当场看见；
            # 真板路径不打，保持基线输出零变化）
            evs = host.events()
            print(f'  模拟源事件流（{len(evs)} 条）：')
            for e in evs:
                print(f'    [{e["seq"]:>3}] {e["level"]:<6} {e["code"]:<14} {e["text"]}')
    loop.dump_csv(os.path.join(OUT, 'wave_session.csv'))
    print('完成。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
