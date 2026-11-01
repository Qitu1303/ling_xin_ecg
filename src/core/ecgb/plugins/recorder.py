"""recorder.py —— 长时记录器（P0，Holter 雏形）。

按 snap.n_total 增量落 CSV（n, code, volt, t_s，天然不重不漏），按 seg_seconds 分段，收尾写
summary.json。回调只把 (base_idx, codes) 丢进有界队列、写盘在 daemon 线程；队列满丢帧计数。
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .. import regs
from .base import PluginBase, register


class _Writer(threading.Thread):
    """后台写盘线程：独占文件句柄，按需分段轮转。"""

    def __init__(self, rec: "Recorder", q: "queue.Queue") -> None:
        super().__init__(name="recorder-writer", daemon=True)
        self.rec = rec
        self.q = q

    def run(self) -> None:                        # pragma: no cover - 线程
        rec = self.rec
        fh = None
        try:
            while True:
                try:
                    item = self.q.get(timeout=1.0)
                except queue.Empty:
                    if fh is not None:
                        fh.flush()
                    continue
                if item is None:
                    break
                base, codes = item
                if fh is None:
                    fh = rec._open_segment(base)
                fs = rec.fs_session
                for i, code in enumerate(codes):
                    idx = base + i
                    if rec._seg_start is not None and fs and \
                            (idx - rec._seg_start) >= rec.params["seg_seconds"] * fs:
                        fh.flush()
                        fh.close()
                        fh = rec._open_segment(idx)
                    volt = code / regs.CODE_FULL / regs.DIVIDER_A0
                    fh.write(f"{idx},{code},{volt:.6f},{(idx / fs) if fs else 0.0:.4f}\n")
                    rec._n_written += 1
        except Exception as exc:                   # 磁盘满/IO 错误：不能静默装死
            rec._dead_reason = repr(exc)
            rec.recording = False
            rec.last_error = f"writer: {exc!r}"
            if rec.ctx is not None:
                rec.ctx.log(f"写线程异常退出：{exc!r}")
        finally:
            if fh is not None:
                try:
                    fh.flush()
                    fh.close()
                except Exception:
                    pass


@register
class Recorder(PluginBase):
    name = "recorder"
    priority = 90                      # 消费类：排在各插件之后
    description = "长时记录器：CSV 分段落盘 + 会话摘要（Holter 雏形）"

    def __init__(self, seg_seconds: float = 600.0, subdir: str = "records",
                 queue_size: int = 16384, start: bool = True) -> None:
        super().__init__(seg_seconds=seg_seconds, subdir=subdir,
                         queue_size=queue_size, start=start)
        self.recording = False
        self.session_dir: Optional[str] = None
        self.fs_session: Optional[float] = None
        self._q: Optional["queue.Queue"] = None
        self._th: Optional[_Writer] = None
        self._last_base: Optional[int] = None
        self._n_written = 0
        self._n_frames_dropped = 0
        self._seg_start: Optional[int] = None
        self._segments: List[str] = []
        self._t_start = 0.0
        self._t_start_wall = ""
        self._last_pub = 0.0
        self._dead_reason = ""
        self._finalizing = False
        self._dead_reported = False
        self._pending_events: List[Tuple[str, str, str]] = []
        self._fin_th: Optional[threading.Thread] = None
        self.session_dir_done: Optional[str] = None

    # ---------------------------------------------------------------- 生命周期
    def on_start(self) -> None:
        os.makedirs(self.ctx.out_dir, exist_ok=True)
        if self.params["start"]:
            self.start_session()

    def start_session(self) -> bool:
        if self.recording:
            return True
        if self._finalizing:                       # 上一次收尾还没做完（异步）→ 稍后重试
            return False
        stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
        self.session_dir = os.path.join(self.ctx.out_dir, self.params["subdir"],
                                        f"session_{stamp}")
        os.makedirs(self.session_dir, exist_ok=True)
        self.fs_session = float(self.ctx.fs or 0.0) or None
        self._q = queue.Queue(maxsize=int(self.params["queue_size"]))
        self._th = _Writer(self, self._q)
        self._th.start()
        self._last_base = None
        self._seg_start = None
        self._segments = []
        self._n_written = 0
        self._n_frames_dropped = 0
        self._dead_reason = ""
        self._t_start = time.monotonic()
        self._t_start_wall = time.strftime("%Y-%m-%d %H:%M:%S")
        self.recording = True
        self.ctx.log(f"开始记录：{self.session_dir}")
        self.ctx.emit_event("info", "开始记录会话", "rec_start", path=self.session_dir)
        self._publish()
        return True

    def stop_session(self, reason: str = "stop") -> None:
        """停止记录：**立即返回**（可在采集线程调用），收尾在后台线程完成。

        写线程可能正卡在磁盘 IO 上，join 最长 3 s —— 绝不能放在采集线程里做，
        否则一次"点停止"就是 3 秒漏样。
        """
        if not self.recording:
            return
        self.recording = False
        self._finalizing = True
        self._fin_th = threading.Thread(target=self._finalize, args=(reason,), daemon=True,
                                        name="recorder-finalize")
        self._fin_th.start()

    def _finalize(self, reason: str) -> None:
        """后台收尾：毒丸 → join（带超时）→ 写 summary.json → 汇报。"""
        if self._q is not None:
            try:
                self._q.put_nowait(None)          # 毒丸：让写线程收尾并退出
            except queue.Full:
                pass
        th = self._th
        if th is not None:
            th.join(timeout=3.0)
        self._th = None
        dur = time.monotonic() - self._t_start
        summary = {
            "session_dir": self.session_dir,
            "fs": self.fs_session,
            "n_written": self._n_written,
            "duration_s": round(dur, 2),
            "frames_dropped": self._n_frames_dropped,
            "segments": list(self._segments),
            "started_wall": self._t_start_wall,
            "ended_wall": time.strftime("%Y-%m-%d %H:%M:%S"),
            "reason": reason,
            "note": "项目仅用于教学演示，不作为医疗诊断用途",
        }
        path = os.path.join(self.session_dir or self.ctx.out_dir, "summary.json")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(summary, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.ctx.log(f"摘要写入失败：{exc!r}")
        self.ctx.log(f"停止记录：{self._n_written} 样本 / {dur:.1f} s")
        # 事件/发布不在此线程直接做（避免与采集线程的 dispatch 并发）；
        # 挂进待办，由下一次 on_snapshot 在采集线程内转发。
        self._pending_events.append(
            ("info", f"记录会话结束（{self._n_written} 样本，{dur:.1f} s）", "rec_stop"))
        self.session_dir_done = self.session_dir
        self._finalizing = False

    def on_close(self) -> None:
        self.stop_session(reason="close")
        th = self._fin_th
        if th is not None and th.is_alive():
            th.join(timeout=3.0)                      # 收尾时可短暂等待（已不在采集循环里）
        self._drain_pending()

    # ---------------------------------------------------------------- 数据通路
    def on_snapshot(self, snap) -> None:
        self._drain_pending()
        if not self.recording or self._q is None:
            return
        if self._dead_reason and not self._dead_reported:
            self._dead_reported = True
            self.ctx.emit_event("medium", f"记录器写盘异常，已停止记录：{self._dead_reason}",
                                "rec_died")
            self._publish()
        wave = snap.wave
        n = int(len(wave)) if wave is not None else 0
        if n == 0:
            return
        n_total = int(snap.n_total)
        base = n_total - n                              # wave[0] 的绝对样本号
        if self._last_base is None:
            # 新会话从"下一个新样本"开始记，不回灌当前整窗（审计 P2）
            self._last_base = n_total
        start_off = self._last_base - base
        if start_off < 0:
            start_off = 0                               # 游标落在窗口之前：整窗都是新的
        if start_off >= n:
            # 本帧没有新样本。★ 注意：滑动窗下 base 永远早于游标，"base < 游标"不是回绕！
            # 只有"整个窗口都比游标旧一个窗口以上"才是会话重启（n_total 归零/换源）。
            if n_total < self._last_base - n:
                self._last_base = base
                start_off = 0
            else:
                return
        codes = wave[start_off:].tolist()
        if not codes:
            return
        self._last_base = base + n                      # = n_total：游标推进到窗口末端
        try:
            self._q.put_nowait((base + start_off, codes))
        except queue.Full:
            self._n_frames_dropped += 1
            self._last_base = base + start_off          # 丢帧但保持索引连续
        now = float(snap.t or 0.0)
        if now - self._last_pub >= 1.0:                   # 每秒给网页/脚本刷一次进度
            self._last_pub = now
            self._publish()

    # ---------------------------------------------------------------- 命令
    def on_command(self, action: str, arg: Any = None) -> Any:
        if action == "start":
            return self.start_session()
        if action == "stop":
            self.stop_session(reason="command")
            return True
        if action == "status":
            return self._latest()
        return None

    # ---------------------------------------------------------------- 工具
    def _drain_pending(self) -> None:
        """在采集线程内转发后台线程挂起的汇报（事件），顺带刷新一次发布。"""
        drained = False
        while self._pending_events:
            level, text, code = self._pending_events.pop(0)
            self.ctx.emit_event(level, text, code, path=self.session_dir_done)
            drained = True
        if drained:
            self._publish()

    def _open_segment(self, base_idx: int):
        self._seg_start = base_idx
        fname = f"part{len(self._segments) + 1:02d}.csv"
        path = os.path.join(self.session_dir or ".", fname)
        new = not os.path.exists(path)
        fh = open(path, "a", encoding="utf-8", buffering=1)
        if new:
            fh.write("n,code,volt,t_s\n")
        self._segments.append(path)
        self.ctx.log(f"分段：{path}")
        return fh

    def _latest(self) -> Dict[str, Any]:
        return {
            "recording": self.recording,
            "session_dir": self.session_dir,
            "n_written": self._n_written,
            "n_frames_dropped": self._n_frames_dropped,
            "segments": len(self._segments),
            "duration_s": round(time.monotonic() - self._t_start, 1) if self.recording else 0.0,
        }

    def _publish(self) -> None:
        self.ctx.publish("recorder", self._latest())

    def stats(self) -> Dict[str, Any]:
        st = super().stats()
        st.update(self._latest())
        return st
