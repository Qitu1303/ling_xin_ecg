# -*- coding: utf-8 -*-
"""HDMI 显示模块：码值波形 → PYNQ HDMI 输出。

硬件约定：帧缓冲字节序为 (G,B,R)，颜色常量已按该序预定义，渲染后不做通道交换。
configure(VideoMode, PIXEL_BGR) → start() → newframe() 顺序固定；update() 默认只存快照即返回。
"""
from __future__ import annotations

import threading
import time
import traceback
from functools import lru_cache

import numpy as np

try:
    from pynq.overlays.base import BaseOverlay
    from pynq.lib.video import VideoMode, PIXEL_BGR
    _PYNQ_OK = True
except Exception:                        # 本机（非板上）import 会失败，属正常
    _PYNQ_OK = False

# 加载 Pillow（图像处理）
try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except Exception:
    _PIL_OK = False

# 颜色常量按帧缓冲字节序 (G,B,R) 预定义（三色标定实测：pos0→绿、pos1→蓝、pos2→红），
# 故渲染后无需整帧通道交换；灰阶常量 (r=g=b) 不受通道顺序影响。
BG       = (0, 0, 0)      # 背景纯黑；清屏走 buf.fill(0)，此常量仅语义保留
WAVE_C   = (255, 0, 0)       # 波形：绿 —— 帧缓冲字节 (255,0,0) 显示为绿
BASE_C   = (70, 70, 70)      # 基线：深灰（灰阶，值不变）
TEXT_C   = (255, 255, 255)   # 主文字：白（灰阶，值不变）
DIM_C    = (160, 160, 160)   # 次要文字：灰（灰阶，值不变）
WAVE_Y0_RATIO = 0.16         # 波形区上边（相对屏高）
WAVE_Y1_RATIO = 0.94         # 波形区下边
CODE_MID  = 32768.0          # 码值中心（uint16 域）
CODE_FULL = 65536.0          # 码值满量程

_FONT_PATHS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
# ttf是linux自带的字体文件，只支持英文


class EcgDisplay:
    """码值波形显示：主循环每帧 update(wave, fs, hr)，后台线程按 max_fps 重绘推屏。

    EcgDisplay() 自建 BaseOverlay("base.bit")（独立冒烟）；EcgDisplay(overlay) 复用已加载 overlay。
    """
    # throttle=节流，max_fps=最大帧率，mv_range=码值满量程；async_render 见模块头
    def __init__(self, overlay=None, width: int = 1280, height: int = 720,
                 throttle: bool = True, max_fps: float = 25.0,
                 mv_range: float = CODE_FULL, async_render: bool = True) -> None:
        if not _PYNQ_OK:
            raise RuntimeError("未检测到 pynq：请在板上运行")
        if not _PIL_OK:
            raise RuntimeError("未检测到 Pillow。")

        self.width, self.height = int(width), int(height)
        if overlay is None:
            overlay = BaseOverlay("base.bit")      # 仅独立冒烟：当前与 AFE 工程 bit 互斥
        self.overlay = overlay
        self.hdmi = overlay.video.hdmi_out

        #  顺序写死：configure → start → newframe（见模块头硬约束）
        self.hdmi.configure(VideoMode(self.width, self.height, 24), PIXEL_BGR)   # 24 = 位深
        self.hdmi.start()
        self.frame = self.hdmi.newframe()          # 帧缓冲取一次，之后每帧复用重绘
        # 以实际拿到的帧缓冲为准（降级分辨率时同理）
        self.height, self.width = int(self.frame.shape[0]), int(self.frame.shape[1])
        # 离屏绘制缓冲：显式申请普通 cached 内存（np.empty_like(self.frame) 会落在
        # VDMA 帧缓冲同一块 uncached CMA 上，全帧 numpy 操作慢 10~20 倍）。
        # 绘制全在 cached 内存完成，最后仅一次 memcpy 写入帧缓冲。
        self._buf = np.empty((self.height, self.width, 3), np.uint8)

        self.throttle = bool(throttle)
        self.max_fps = float(max_fps)
        self.mv_range = float(mv_range)
        self._last_push = 0.0
        self.n_frames = 0                          # 实际推屏帧数（渲染路径累加）

        # ---- 采集/渲染解耦：最新快照 + 后台渲染线程（async_render=True）----
        self.async_render = bool(async_render)
        self._snap = None                          # (wave|None, fs, hr) 最新快照
        self._snap_seq = 0                         # 快照序号：渲染线程据此只渲染新数据
        self._snap_lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._render_thread = None
        self.n_render_err = 0                      # 渲染异常计数（自吞，不拖采集）

        self._font_big = self._load_font(30)
        self._font_small = self._load_font(18)

        y0 = int(self.height * WAVE_Y0_RATIO)
        y1 = int(self.height * WAVE_Y1_RATIO)
        self._y0, self._y1 = y0, y1
        self._y_mid = (y0 + y1) // 2
        self._half_span = (y1 - y0) / 2.0

        if self.async_render:
            self._render_thread = threading.Thread(
                target=self._render_loop, name="ecg_display-render", daemon=True)
            self._render_thread.start()

        print(f"[ecg_display] 就绪：{self.width}x{self.height} | 波形区 y=[{y0},{y1}] | "
              f"mv_range={self.mv_range:g} | throttle={self.throttle} max_fps={self.max_fps:g} | "
              f"async_render={self.async_render}", flush=True)

    # ---- 内部 ----

    @staticmethod        # 静态方法，无需实例化
    @lru_cache(maxsize=16)
    def _load_font(size: int):
        for p in _FONT_PATHS:
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
        try:
            return ImageFont.load_default(size)    # Pillow ≥10 支持带字号
        except TypeError:
            return ImageFont.load_default()

    def _map_y(self, wave) -> np.ndarray:
        """码值 → 屏幕行号：以 32768 为中线，±mv_range/2 映射到波形区，越界 clamp。"""
        v = np.asarray(wave)
        if v.dtype.kind == "i" and v.size and int(v.min()) < 0:
            v = v.astype(np.int32) & 0xFFFF        # 兼容 int16 转储 → 还原无符号码值域
        v = v.astype(np.float32)
        y = self._y_mid - (v - CODE_MID) / (self.mv_range / 2.0) * self._half_span
        return np.clip(y, self._y0, self._y1)

    # ---- 对外 ----

    def update(self, wave, fs: float, hr) -> bool:
        """每帧调用：存快照并立即返回（async_render=True）；同步模式下真正推屏。

        wave : 一维码值数组（0..65535，32768 ≈ 基线；dtype 建议 uint16，int16 亦兼容）。
        fs   : 采样率 Hz；hr : 心率 int 或 None。
        契约红线：不读寄存器、不改入参、不回调、不做信号/心率判断。
        """
        if self.async_render:
            snap = None if wave is None else np.asarray(wave).copy()   # 微秒级，断开调用方 buffer
            with self._snap_lock:
                self._snap = (snap, fs, hr)
                self._snap_seq += 1
            return True

        # ---- 同步模式（async_render=False 的回退路径）----
        if self.throttle:
            now = time.monotonic()
            if now - self._last_push < 1.0 / self.max_fps:
                return False
        return self._render_frame(wave, fs, hr)

    def _render_frame(self, wave, fs: float, hr) -> bool:
        """渲染一帧并推屏（仅渲染线程/同步路径调用，帧缓冲只由本路径访问）。

        全部绘制在离屏缓冲完成，最后一次 memcpy 上屏，把撕裂窗口从整帧时长压到 ~ms。
        """
        buf = self._buf
        # 画上波形（离屏）
        buf.fill(0)     # 纯黑清屏走 memset 快路径（等价 BG）；元组广播在板上慢约 50 倍
        n = 0 if wave is None else int(len(wave))
        if n >= 2:
            ys = self._map_y(wave)
            x = np.linspace(0.0, self.width - 1.0, n)
            ycol = np.interp(np.arange(self.width), x, ys).astype(np.int32)
            # flat 散射法：相邻两列的竖线段 = 行号落在 [lo,hi] 闭区间的像素，
            # 一次性 repeat 成 flat 索引后用 fancy 散射写入，代价正比波形像素数（非全图 H·W）。
            # ycol 经 _map_y 的 clip + int32 保证落在 [0, height-1] 界内。
            lo = np.minimum(ycol[:-1], ycol[1:])               # (W-1,) 每段上端
            hi = np.maximum(ycol[:-1], ycol[1:])               # (W-1,) 每段下端
            seg = hi - lo + 1                                  # (W-1,) 每段像素数（含端点）
            col_idx = np.repeat(np.arange(self.width - 1), seg)
            row_idx = np.repeat(lo, seg) + (np.arange(int(seg.sum())) - np.repeat(np.cumsum(seg) - seg, seg))
            buf[row_idx, col_idx] = WAVE_C                     # 一次 fancy 散射写全部波形像素
        buf[self._y_mid, :, :] = BASE_C             # 基线

        img = Image.fromarray(buf)                  # PIL 直接读离屏字节

        # 画上文字
        d = ImageDraw.Draw(img)
        d.text((18, 10), f"HR {int(hr)} BPM" if hr is not None else "HR --",
               fill=TEXT_C, font=self._font_big)
        d.text((18, 50), f"fs = {fs:g} S/s    N = {n}", fill=DIM_C, font=self._font_small)
        d.text((self.width - 200, 12), f"frame {self.n_frames}", fill=DIM_C,
               font=self._font_small)

        arr = np.asarray(img)                       # 即最终帧缓冲字节序（常量已预定义，无需交换）
        f = self.frame
        f[:] = arr                                  # ★ 单次 memcpy 上屏（撕裂窗口 ~ms）
        self.hdmi.writeframe(f)                     # 整帧推送（非增量）
        self.n_frames += 1
        self._last_push = time.monotonic()
        return True

    def _render_loop(self) -> None:
        """后台渲染线程：有新快照才渲染，按 max_fps 节流；异常自吞（不拖垮采集）。"""
        last_seq = 0
        while not self._stop_evt.is_set():
            t0 = time.monotonic()
            with self._snap_lock:
                snap, seq = self._snap, self._snap_seq
            if snap is not None and seq != last_seq:   # 没新快照就空转等待，不重复推屏
                last_seq = seq
                try:
                    self._render_frame(*snap)
                except Exception:
                    self.n_render_err += 1
                    if self.n_render_err <= 3:        # 只打前几次，避免刷屏
                        print(f"[ecg_display] 渲染线程异常（第 {self.n_render_err} 次）：",
                              flush=True)
                        traceback.print_exc()
            elapsed = time.monotonic() - t0
            self._stop_evt.wait(max(0.0, 1.0 / self.max_fps - elapsed))

    def close(self) -> None:
        """收尾：停渲染线程 + 停 HDMI 输出（幂等）。"""
        self._stop_evt.set()
        t = self._render_thread
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
        self._render_thread = None
        try:
            self.hdmi.stop()
        except Exception:
            pass


# ---- 自测（上板运行）----

def _self_test(frames: int = 30) -> None:
    fs = 31.25
    n_win = 125                                   # 窗口 ≈4 s，贴合主程序 deque(maxlen≈250) 口径
    t = np.arange(n_win) / fs
    t_start = time.time()

    print("=" * 72, flush=True)
    print("[1/5] 合成数据：1.2 Hz 正弦 + 噪声（fs=31.25 S/s，窗口 4 s，假装 72 BPM）", flush=True)

    def make_wave(phase: float) -> np.ndarray:
        w = (CODE_MID
             + 6000.0 * np.sin(2 * np.pi * 1.2 * (t + phase))
             + np.random.randn(n_win) * 120.0)
        return np.clip(w, 0, 65535).astype(np.uint16)

    print("[2/5] 建立 EcgDisplay()（自建 BaseOverlay('base.bit')；HDMI OUT 需已接显示器/采集卡）",
          flush=True)
    disp = EcgDisplay()

    print(f"[3/5] 推 {frames} 帧（≈{frames / 20.0:.1f} s @ 20 fps 调用；"
          f"async_render={disp.async_render}，内部渲染上限 {disp.max_fps:g} fps）……",
          flush=True)
    pushed = 0
    for i in range(frames):
        ok = disp.update(make_wave(i / fs), fs, 72)
        pushed += int(ok)
        if i % 5 == 0:
            print(f"       frame {i:3d} | 已接收={ok} | 累计接收 {pushed}"
                  f" | 实际推屏 {disp.n_frames}", flush=True)
        time.sleep(0.05)

    time.sleep(0.6)                                   # 异步模式：等渲染线程把最后一帧推完
    print(f"[4/5] 完成：累计接收 {pushed}/{frames} 帧，实际推屏 {disp.n_frames} 帧"
          f"（耗时 {time.time() - t_start:.1f} s）", flush=True)
    print("[5/5] close()", flush=True)
    disp.close()

    print("\n怎么判读（照着对）:", flush=True)
    print("  ・显示器 / PC(OBS) 看到 绿色波形滚动 + 'HR 72 BPM' + 'fs = 31.25 S/s' → 冒烟通过",
          flush=True)
    print("  ・异步模式下「实际推屏」略少于「累计接收」属正常（渲染线程只画最新快照，自动跳帧）",
          flush=True)
    print("  ・看不到但终端无异常 → ① HDMI 插的是 OUT 口吗 ② 采集卡侧 OBS 分辨率设 1280×720"
          " ③ 速通文档 §5 排查树", flush=True)
    print("  ・报 _cache → 不该发生（内部顺序已写死）→ 把完整输出发给 C", flush=True)
    print("  ・如何判活 → 波形在动/帧计数在涨 = 通路活着（静态画面无法区分卡帧）", flush=True)


if __name__ == "__main__":
    if not _PYNQ_OK:
        print("[ecg_display] 未检测到 pynq：本脚本必须在板上运行（Jupyter / 板载 python3）。")
        print("              本机（无 pynq）只能做语法检查：python -m py_compile ecg_display.py")
        raise SystemExit(1)
    try:
        _self_test()
    except Exception:
        print("\n❌ 自测中断，异常如下（对照上面的 [n/5] 定位，别一次改两处）：", flush=True)
        traceback.print_exc()
        raise SystemExit(1)
