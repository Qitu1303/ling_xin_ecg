"""voice.py —— 语音/蜂鸣提示（P0，零接口改动）。

把宿主事件 high/medium/low 翻译成不同音高的提示音（880×3 / 660×2 / 520×1）；后端运行时探测
aplay / winsound / 仅日志。防刷屏：同级别最小间隔 min_gap 秒；info 级别不发声。
"""
from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict

from .base import PluginBase, register

_RATE = 16000
_BEEP_PATTERNS = {
    "high": ((880.0, 0.12), (0.06,), (880.0, 0.12), (0.06,), (880.0, 0.16)),
    "medium": ((660.0, 0.14), (0.08,), (660.0, 0.14)),
    "low": ((520.0, 0.18),),
}


@register
class VoicePrompts(PluginBase):
    name = "voice"
    priority = 99                     # 最后执行：等所有分析插件先出结论
    description = "语音/蜂鸣提示（按报警级别发声）"

    def __init__(self, min_gap: float = 1.2, volume: float = 0.35,
                 enable: bool = True, levels: str = "high,medium,low") -> None:
        super().__init__(min_gap=min_gap, volume=volume, enable=enable, levels=levels)
        self._wav_cache: Dict[str, bytes] = {}
        self._wav_path: Dict[str, str] = {}
        self._last_played: Dict[str, float] = {}
        self._last_proc = None
        self._aplay = shutil.which("aplay") if sys.platform.startswith("linux") else None
        self._winsound = None
        if sys.platform.startswith("win"):
            try:
                import winsound  # type: ignore
                self._winsound = winsound
            except Exception:
                self._winsound = None
        self._backend = "aplay" if self._aplay else ("winsound" if self._winsound else "log")
        self._allowed = {s.strip() for s in str(levels).split(",") if s.strip()}

    # ---------------------------------------------------------------- 生命周期
    def on_start(self) -> None:
        if not self.params["enable"]:
            self.ctx.log("语音提示已禁用（enable=false）")
            return
        for level in ("high", "medium", "low"):     # 预生成：合成不进事件回调（审计 P2）
            self._pattern_wav(level)
        self.ctx.log(f"语音提示后端：{self._backend}"
                     + ("（未找到音频设备，仅记录日志）" if self._backend == "log" else ""))

    def on_event(self, event: Dict[str, Any]) -> None:
        if not self.params["enable"]:
            return
        level = str(event.get("level") or "")
        if level == "info" or level not in self._allowed:
            return
        now = time.monotonic()
        if now - self._last_played.get(level, 0.0) < self.params["min_gap"]:
            return
        self._last_played[level] = now
        data = self._pattern_wav(level)
        threading.Thread(target=self._play, args=(level, data), daemon=True,
                         name=f"voice-{level}").start()

    def on_close(self) -> None:
        proc = self._last_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        for p in self._wav_path.values():
            try:
                os.unlink(p)
            except Exception:
                pass

    # ---------------------------------------------------------------- 合成与播放
    def _pattern_wav(self, level: str) -> bytes:
        if level in self._wav_cache:
            return self._wav_cache[level]
        parts = _BEEP_PATTERNS.get(level) or _BEEP_PATTERNS["low"]
        frames = bytearray()
        amp = int(32767 * float(self.params["volume"]))
        for seg in parts:
            if len(seg) == 1:                      # 间隔
                n = int(seg[0] * _RATE)
                frames.extend(b"\x00\x00" * n)
                continue
            freq, dur = seg
            n = int(dur * _RATE)
            for i in range(n):
                # 两端 5 ms 淡入淡出，避免爆音
                env = min(1.0, i / (0.005 * _RATE), (n - i) / (0.005 * _RATE))
                v = int(amp * env * math.sin(2.0 * math.pi * freq * i / _RATE))
                frames.extend(struct.pack("<h", v))
        buf = _to_wav_bytes(bytes(frames))
        self._wav_cache[level] = buf
        return buf

    def _play(self, level: str, data: bytes) -> None:      # pragma: no cover - 设备相关
        try:
            if self._aplay:
                path = self._wav_path.get(level)
                if path is None:
                    fd, path = tempfile.mkstemp(prefix=f"ecg_beep_{level}_", suffix=".wav")
                    os.close(fd)
                    with open(path, "wb") as fh:
                        fh.write(data)
                    self._wav_path[level] = path
                proc = subprocess.Popen([self._aplay, "-q", path],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._last_proc = proc
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:   # pragma: no cover - 设备卡死
                    proc.terminate()
            elif self._winsound is not None:
                # Windows：SND_MEMORY 与 SND_ASYNC 不能同时用（RuntimeError）；
                # 本函数已跑在专用后台线程里，同步播放即可，不阻塞采集。
                self._winsound.PlaySound(data, self._winsound.SND_MEMORY)
            else:
                self.ctx.log(f"[提示音 {level}]（无音频后端，仅日志）")
        except Exception as exc:
            self.ctx.log(f"提示音播放失败：{exc!r}")

    def stats(self) -> Dict[str, Any]:
        st = super().stats()
        st["backend"] = self._backend
        return st


def _to_wav_bytes(pcm: bytes) -> bytes:
    """把 16-bit 单声道 PCM 包成 WAV（标准库 wave 只支持写文件，故手写头）。"""
    hdr = bytearray()
    hdr.extend(b"RIFF")
    hdr.extend(struct.pack("<I", 36 + len(pcm)))
    hdr.extend(b"WAVEfmt ")
    hdr.extend(struct.pack("<IHHIIHH", 16, 1, 1, _RATE, _RATE * 2, 2, 16))
    hdr.extend(b"data")
    hdr.extend(struct.pack("<I", len(pcm)))
    return bytes(hdr) + pcm
