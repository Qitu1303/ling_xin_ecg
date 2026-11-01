"""display_link.py —— B→C 显示对接层（薄适配，C 的 ecg_display.py 一个字不改）。

契约：update(wave, fs, hr)，wave 必须 uint16 一维、长度 >=2，hr 为 int 或 None；fs 禁止写死
（取 push 实参 → 构造/上次合法值，拿不到就拒发这一帧）；显示侧异常一律吞掉并记账。
"""
from __future__ import annotations

import math
import traceback
from typing import Any, Optional, Tuple

import numpy as np

__all__ = ["DisplayLink", "NullDisplay", "WAVE_MIN_LEN", "FS_MIN", "FS_MAX"]

# ---- 冻结契约的边界（改这里等于改接口，先改 docs/接口表_冻结版.md）----
WAVE_MIN_LEN = 2          # C 内部 n>=2 才连线；短于此没意义，直接拦下
FS_MIN = 1.0              # Hz；低于 1 说明 fs 还没标定出来
FS_MAX = 1000.0           # Hz；板上实测量级 19.53~31.25，超上限只可能是误传


def _valid_fs(fs: Any) -> Optional[float]:
    """把 fs 归一成 [FS_MIN, FS_MAX] 内的 float；拿不准返回 None，由调用方决定兜底。

    bool 单独挡掉：True 会被当成 1 Hz 混进来，那是误传，不是采样率。
    """
    if fs is None or isinstance(fs, bool):
        return None
    if not isinstance(fs, (int, float, np.integer, np.floating)):
        return None
    try:
        v = float(fs)
    except (TypeError, ValueError):          # 防御性分支，正常类型到不了
        return None
    if not math.isfinite(v) or v < FS_MIN or v > FS_MAX:
        return None
    return v


def _is_int(v: Any) -> bool:
    """hr 的合法类型：int / np.integer；bool 不算（True 不是心率）。"""
    return isinstance(v, (int, np.integer)) and not isinstance(v, bool)


class NullDisplay:
    """无 pynq / 无 HDMI 时的显示替身：只记账，不画屏。

    与 C 的 EcgDisplay 同形（update(wave, fs, hr) -> bool、close()），DisplayLink 不必为它加分支。
    """

    def __init__(self) -> None:
        self.n_frames = 0
        self.n_pushed = 0
        self.last_hr: Optional[int] = None
        self.last_fs: Optional[float] = None
        self.last_len = 0
        self.last_min: Optional[int] = None
        self.last_max: Optional[int] = None
        self.closed = False
        self.close_calls = 0

    def update(self, wave, fs: float, hr) -> bool:
        w = np.asarray(wave)
        self.n_frames += 1
        self.n_pushed += 1
        self.last_hr = None if hr is None else int(hr)
        self.last_fs = float(fs)
        self.last_len = int(w.size)
        self.last_min = int(w.min()) if w.size else None
        self.last_max = int(w.max()) if w.size else None
        return True

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    def __repr__(self) -> str:
        return (f"NullDisplay(frames={self.n_frames}, hr={self.last_hr}, len={self.last_len}, "
                f"range=[{self.last_min}, {self.last_max}])")


class DisplayLink:
    """B→C 的薄适配层：校验 → 转交 → 记账，单帧异常永不外泄。

    disp: None 时自动 import ecg_display 建 EcgDisplay()，失败退到 NullDisplay；也可传实例或类。
    fs: 已知/标定好的采样率 (Hz)，作为 push() 未带 fs 时的兜底；None = 只能靠 push(fs=...) 推帧。
    window_sec / fps / verbose 只用于提示，不做节流（真正的节流在 C 内部）。
    """

    def __init__(self, disp: Any = None, fs: Any = None, window_sec: float = 8.0,
                 fps: float = 20.0, verbose: bool = True) -> None:
        self.verbose = bool(verbose)
        self.window_sec = max(float(window_sec), 0.0)
        self.fps = max(float(fps), 0.0)

        self.fs = _valid_fs(fs)                 # 后续被每一帧的实测值刷新
        ctor_fs_bad = fs is not None and self.fs is None

        # ---- 统计：n_frames == n_pushed + n_skipped 恒成立（stats() 里断言）----
        self.n_frames = 0        # push() 调用次数
        self.n_pushed = 0        # C 返回 True 的帧数
        self.n_skipped = 0       # 没进 C 的帧数 = 被拦 + 节流 + 异常
        self.n_rejected = 0      #   子集：参数校验没过
        self.n_throttled = 0     #   子集：C 主动返回 False（节流）
        self.n_errors = 0        #   子集：C 抛异常 / 返回 None
        self.n_fs_fallback = 0   #   子集：这一帧的 fs 用了兜底值
        self.last_error: Optional[str] = None   # 最近一次 reject 原因或异常 traceback
        self._closed = False
        self._warned_len = False
        self._warned_fs = False

        self.disp, self.backend = self._make_disp(disp)
        if ctor_fs_bad:
            self.last_error = f"[config] 构造 fs={fs!r} 非法，已按未标定处理"

        if self.verbose:
            fs_txt = "未标定（每帧必须带 fs）" if self.fs is None else f"{self.fs:g} S/s"
            print(f"[display_link] 就绪：后端={self.backend} | fs={fs_txt} | "
                  f"window={self.window_sec:g}s | 期望调用≈{self.fps:g} fps（节流在 C 内部）",
                  flush=True)
            note = getattr(self, "backend_note", None)
            if note:
                print(f"[display_link] 注：{note}", flush=True)

    # ------------------------------ 后端选择 ------------------------------

    def _make_disp(self, disp: Any) -> Tuple[Any, str]:
        """选定后端。显式传入的必须可用（错了当场抛）；只有自动模式允许静默退替身。"""
        if disp is not None:
            inst = disp
            if isinstance(disp, type):               # 传类 → 本层帮忙实例化
                try:
                    inst = disp()
                except Exception as exc:
                    raise TypeError(f"实例化 {disp.__name__} 失败：{exc!r}") from exc
            if not callable(getattr(inst, "update", None)):
                raise TypeError(
                    f"disp={type(inst).__name__} 没有 update(wave, fs, hr) 方法；"
                    "按 docs/接口表_冻结版.md §4 传 C 的 EcgDisplay 实例")
            return inst, f"injected:{type(inst).__name__}"

        try:
            from ecg_display import EcgDisplay       # 板上：C 的模块在 sys.path 上
        except Exception as exc:
            return self._null_fallback(f"import ecg_display 失败（本机无 pynq 属正常）：{exc!r}")
        try:
            inst = EcgDisplay()                      # 无参 = 独立冒烟模式（自建 base.bit）
        except Exception as exc:
            return self._null_fallback(f"EcgDisplay() 建立失败：{exc!r}")
        if not callable(getattr(inst, "update", None)):
            return self._null_fallback("EcgDisplay 实例没有 update()，接口对不上")
        return inst, "ecg_display.EcgDisplay"

    def _null_fallback(self, note: str) -> Tuple["NullDisplay", str]:
        """自动模式下任何失败都退到 NullDisplay：本机要能跑，板上换真件不改调用方。"""
        self.backend_note = note
        return NullDisplay(), "NullDisplay"

    # ------------------------------ 对外 ------------------------------

    def push(self, wave, hr, fs: Any = None) -> bool:
        """推一帧快照；返回「这一帧是否真的进了 C」。

        校验不过就丢帧、记账并返回 False（不抛异常）：
            wave : np.ndarray，dtype=uint16（冻结契约），一维，长度 >=2
            hr   : int / np.integer，或 None（未检出 → C 画 "HR --"）
            fs   : 本次实测采样率（Hz，1~1000）；None/非法 → 用 self.fs 兜底
        """
        self.n_frames += 1

        if self._closed:
            return self._reject("close() 之后不再推帧（显示后端已收尾）")

        bad = self._check_wave(wave)
        if bad is not None:
            return self._reject(bad)
        if not (hr is None or _is_int(hr)):
            return self._reject(f"hr 必须是 int 或 None（冻结契约），收到 {type(hr).__name__}={hr!r}")

        fs_out, used_fallback = self._resolve_fs(fs)
        if fs_out is None:
            return self._reject(
                f"fs 缺失或非法（push fs={fs!r}，构造/记忆 fs={self.fs!r}）：合法范围 "
                f"{FS_MIN:g}~{FS_MAX:g} Hz，绝不给 C 传 0/None")
        if used_fallback:
            self.n_fs_fallback += 1
            if self.verbose and not self._warned_fs:
                self._warned_fs = True
                print(f"[display_link] 提示：push fs={fs!r} 不可用，本帧用兜底 fs={fs_out:g} S/s"
                      "（画面文字层按兜底值标注，请检查 ps_afe.measure_rate 的口径）", flush=True)
        self.fs = fs_out                          # 记住最近一次合法 fs，作为下一帧兜底

        n = int(wave.size)
        exp = self.expected_n(fs_out)
        if self.verbose and not self._warned_len and exp and n < 0.5 * exp:
            self._warned_len = True
            print(f"[display_link] 提示：wave 长度 {n} 远小于 fs*window_sec={exp}"
                  f"（{fs_out:g} S/s × {self.window_sec:g}s），画面会被拉宽", flush=True)

        hr_out = None if hr is None else int(hr)
        try:
            # copy：C 即便原地改数组也碰不到调用方的 buffer（红线：不改入参）
            res = self.disp.update(wave.copy(), fs_out, hr_out)
        except Exception:
            return self._record_exc()

        if res is None:                           # 契约要求显式 bool，None 不能当节流混过去
            self.n_errors += 1
            self.n_skipped += 1
            self.last_error = ("[contract] disp.update() 返回 None，应为 bool"
                               "（节流命中必须显式返回 False）")
            if self.verbose:
                print("[display_link] " + self.last_error, flush=True)
            return False

        ok = bool(res)
        if ok:
            self.n_pushed += 1
        else:
            self.n_throttled += 1
            self.n_skipped += 1
        return ok

    def close(self) -> None:
        """收尾：调一次 C 的 close()，重复调用直接返回（幂等）；异常照吞。"""
        if self._closed:
            return
        self._closed = True
        closer = getattr(self.disp, "close", None)
        if not callable(closer):
            return
        try:
            closer()
        except Exception:
            self.n_errors += 1
            self.last_error = traceback.format_exc()
            if self.verbose:
                print("[display_link] close() 抛异常，已吞掉（不影响退出）", flush=True)

    # ------------------------------ 内部 ------------------------------

    @staticmethod
    def _check_wave(wave) -> Optional[str]:
        """契约校验：返回错误描述（None = 通过）。"""
        if wave is None:
            return "wave 是 None（快照还没产出？）"
        if not isinstance(wave, np.ndarray):
            return (f"wave 必须是 np.ndarray，收到 {type(wave).__name__}；"
                    "用 np.asarray(x, dtype=np.uint16) 转一下")
        if wave.dtype != np.uint16:
            return (f"wave dtype 必须是 uint16（冻结契约），收到 {wave.dtype}；"
                    "int16 请 astype(np.uint16) 后再传")
        if wave.ndim != 1:
            return f"wave 必须是一维（按时间正序），收到 ndim={wave.ndim}"
        if wave.size < WAVE_MIN_LEN:
            return f"wave 长度必须 ≥{WAVE_MIN_LEN}，收到 {wave.size}"
        return None

    def _resolve_fs(self, fs: Any) -> Tuple[Optional[float], bool]:
        """fs 兜底链：push 实参 → 记住的/构造的 fs。返回 (值, 是否用了兜底)。"""
        v = _valid_fs(fs)
        if v is not None:
            return v, False
        fallback = self.fs if _valid_fs(self.fs) is not None else None
        if fallback is not None:
            return float(fallback), True
        return None, False

    def _reject(self, reason: str) -> bool:
        self.n_rejected += 1
        self.n_skipped += 1
        self.last_error = f"[reject] {reason}"
        if self.verbose:
            print(f"[display_link] 丢帧：{reason}", flush=True)
        return False

    def _record_exc(self) -> bool:
        """单帧异常：记账 + 留 traceback，主循环继续跑。"""
        self.n_errors += 1
        self.n_skipped += 1
        self.last_error = traceback.format_exc()
        if self.verbose:
            print("[display_link] 显示后端本帧异常，已吞掉（采集继续）；traceback 见 last_error",
                  flush=True)
        return False

    def expected_n(self, fs: Any = None) -> int:
        """按 fs*window_sec 估算期望快照长度（冻结契约建议 N = round(fs*8)）。"""
        v = _valid_fs(self.fs if fs is None else fs)
        if v is None or self.window_sec <= 0:
            return 0
        return int(round(v * self.window_sec))

    def stats(self) -> dict:
        """给主循环 / 日志用的一份统计快照。"""
        assert self.n_frames == self.n_pushed + self.n_skipped, "统计不变量被破坏"
        return {
            "backend": self.backend,
            "closed": self._closed,
            "fs": self.fs,
            "n_frames": self.n_frames,
            "n_pushed": self.n_pushed,
            "n_skipped": self.n_skipped,
            "n_rejected": self.n_rejected,
            "n_throttled": self.n_throttled,
            "n_errors": self.n_errors,
            "n_fs_fallback": self.n_fs_fallback,
            "last_error": self.last_error,
        }
