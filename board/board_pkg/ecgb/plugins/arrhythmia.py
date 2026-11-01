"""arrhythmia.py —— 心律失常初筛（P1；规则为**演示级**，非诊断）。

规则：过速 / 过缓 / 停搏 / 疑似漏搏 / 节律不齐；低速码需凑够 confirm_windows 个独立窗且持续
>= confirm_span_s 才升级为事件，pause 走即时通道。关键码：`missed` 与 `missed_suspect`(low)
由三指纹判别（E1 RR≈2× 且 E2 影子峰 / E3 幅值 cv）；质量门只把低危码降成 device_suspect。
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np

from .base import PluginBase, beats_of, register


@register
class Arrhythmia(PluginBase):
    name = "arrhythmia"
    priority = 40
    description = "心律失常初筛：过速/过缓/停搏/漏搏/节律不齐"

    def __init__(self, tachy: float = 100.0, brady: float = 50.0,
                 tachy_extreme: float = 150.0, brady_extreme: float = 40.0,
                 pause_s: float = 2.0, pause_extreme_s: float = 3.0,
                 miss_factor: float = 1.6, irreg_cv: float = 0.15,
                 irregular_n: int = 12, cooldown: float = 6.0,
                 update_sec: float = 0.5,
                 # ---- 只增不改：旧参数名一个都没动 ----
                 # 【C】两段式减速
                 confirm_windows: int = 3, confirm_span_s: float = 2.5,
                 instant_codes: str = "pause", instant_recheck: int = 1,
                 instant_lookback_beats: int = 2, cand_timeout_s: float = 12.0,
                 # 【B2】中位口径"漏搏摊平"（把 2 倍间隔摊成两段再取中位）
                 flatten_med_enable: bool = True, flatten_q: float = 0.25,
                 flatten_band_lo: float = 1.6, flatten_band_hi: float = 2.4,
                 flatten_tol: float = 0.15,
                 # 【B】漏检三指纹（E1 前置 + E2/E3 取证）
                 suspect_enable: bool = True, amp_shadow: float = 0.35,
                 amp_cv_suspect: float = 0.45, amp_nbhd_sec: float = 0.12,
                 # 【A】轻量质量门（只挡低危码；缺数据一律放行）
                 untrusted_enable: bool = True, fs_dev_max: float = 0.10,
                 sqi_min: float = 0.30, ok_ratio_min: float = 0.5,
                 trust_win_sec: float = 6.0,
                 # 【A-2】连续漏检占比维度（只增不改）
                 miss_ratio_max: float = 0.35,
                 # 【收尾】取证窗半宽地板（只增不改）：0.10 = 旧硬编码口径，0.20 = 放宽后的值。
                 gap_win_half_s: float = 0.20) -> None:
        super().__init__(tachy=tachy, brady=brady, tachy_extreme=tachy_extreme,
                         brady_extreme=brady_extreme, pause_s=pause_s,
                         pause_extreme_s=pause_extreme_s, miss_factor=miss_factor,
                         irreg_cv=irreg_cv, irregular_n=irregular_n,
                         cooldown=cooldown, update_sec=update_sec,
                         confirm_windows=confirm_windows, confirm_span_s=confirm_span_s,
                         instant_codes=instant_codes, instant_recheck=instant_recheck,
                         instant_lookback_beats=instant_lookback_beats,
                         cand_timeout_s=cand_timeout_s,
                         flatten_med_enable=flatten_med_enable, flatten_q=flatten_q,
                         flatten_band_lo=flatten_band_lo, flatten_band_hi=flatten_band_hi,
                         flatten_tol=flatten_tol,
                         suspect_enable=suspect_enable, amp_shadow=amp_shadow,
                         amp_cv_suspect=amp_cv_suspect, amp_nbhd_sec=amp_nbhd_sec,
                         untrusted_enable=untrusted_enable, fs_dev_max=fs_dev_max,
                         sqi_min=sqi_min, ok_ratio_min=ok_ratio_min,
                         trust_win_sec=trust_win_sec,
                         miss_ratio_max=miss_ratio_max,
                         gap_win_half_s=gap_win_half_s)
        self._rr: Deque[float] = deque(maxlen=256)
        self._last_idx: Optional[int] = None
        self._last_eval = 0.0
        self._cool: Dict[str, float] = {}
        self._sticky: Dict[str, tuple] = {}          # code → (level, 过期时刻)，防"一闪而过"
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=20)
        self._latest: Dict[str, Any] = {"status": "等待数据", "level": "info"}
        # 【C】候选登记表：code → {first,last,n_win,last_beat,level,text}
        self._cand: Dict[str, Dict[str, Any]] = {}
        # 【B】漏检取证调试证据（只读，进 latest["miss_dbg"]）
        self._miss_dbg: Dict[str, Any] = {}
        # 【回补2】间隙取证调试证据（只读，进 latest["gap_dbg"]；只增字段）
        self._gap_dbg: Dict[str, Any] = {}
        # 【A】质量门：hr_ok 滑窗 + 不可信防抖（连续 2 次评估才生效，硬编码不参数化）
        self._ok_hist: Deque[Tuple[float, bool]] = deque(maxlen=24)
        self._trust_ok = True
        self._trust_why: List[str] = []
        self._untrust_n = 0            # 连续不可信评估次数（防抖用）
        self._untrust_total = 0        # 累计不可信评估次数（latest.trust.n）
        self._recal_seen = 0           # 已见过的 recal_count（变化即清零计时）

    # ---------------------------------------------------------------- 数据
    def on_snapshot(self, snap) -> None:
        # 【A】hr_ok 滑窗（每帧都记，与拍序列无关：拍缺失时质量门的判据照样要更新）
        t_now = float(getattr(snap, "t", 0.0) or 0.0)
        self._ok_hist.append((t_now, bool(getattr(snap, "hr_ok", False))))
        cut = t_now - float(self.params["trust_win_sec"])
        while self._ok_hist and self._ok_hist[0][0] < cut:
            self._ok_hist.popleft()
        b = beats_of(snap)
        if not b:
            return
        idx = list(b.get("idx_abs") or ())
        t_s = list(b.get("t_s") or ())
        if len(idx) < 2 or len(t_s) != len(idx):
            return
        new = 0
        for k in range(1, len(idx)):
            a = int(idx[k])
            if self._last_idx is not None and a <= self._last_idx:
                continue
            rr = (float(t_s[k]) - float(t_s[k - 1])) * 1000.0
            # 量程上限必须高于 longest pause 阈值，否则"真停搏"会被前置过滤掉
            if 200.0 <= rr <= 15000.0:
                self._rr.append(rr)
                new += 1
        if idx:
            self._last_idx = int(idx[-1])
        if not new:
            return
        if float(snap.t) - self._last_eval < self.params["update_sec"]:
            return
        self._last_eval = float(snap.t)
        self._cand_tick(time.monotonic())        # 【C】候选过期清理（早于 _evaluate）
        self._evaluate(snap)

    # ---------------------------------------------------------------- 判定
    def _evaluate(self, snap) -> None:
        if len(self._rr) < 4:
            return
        p = self.params
        now = time.monotonic()
        rr = np.asarray(self._rr, dtype=np.float64)
        self._update_trust(snap)                     # 【A】质量门判定（含防抖/重标定清零）
        med_raw = float(np.median(rr[-8:]))          # 原始中位（missed/量程判据用）
        med = float(np.median(self._flatten(rr[-8:])))   # 【B2】摊平后中位（bpm 用）
        bpm = 60000.0 / max(med, 1e-9)
        last_rr = float(rr[-1])
        # 回看口径（计划 §2.1 口径 2）：长间歇的证据在长 RR 那一窗出现后，下一个新拍
        # 到来时 last_rr 已回到正常值 —— 若只认 last_rr，pause 永远凑不出"连续 2 次评估"，
        # 真停搏就会永不升级（踩红线）。故 pause 判据用 max(rr[-lookback:])。
        lb = max(1, int(p["instant_lookback_beats"]))
        rr_now = float(np.max(rr[-lb:]))
        beat_idx = int(self._last_idx or 0)      # 绝对样本号（idx_abs[-1]）
        # 本窗成立集合：code → (level, text)；统一在下面一次性过 _offer
        est: Dict[str, Tuple[str, str]] = {}

        if bpm >= p["tachy_extreme"]:
            est["tachy"] = ("high", f"心动过速：约 {bpm:.0f} bpm（极端区）")
        elif bpm >= p["tachy"]:
            est["tachy"] = ("medium", f"心动过速：约 {bpm:.0f} bpm")
        if bpm <= p["brady_extreme"]:
            est["brady"] = ("high", f"心动过缓：约 {bpm:.0f} bpm（极端区）")
        elif bpm <= p["brady"]:
            est["brady"] = ("medium", f"心动过缓：约 {bpm:.0f} bpm")

        # 长间歇**先作间隙取证**再决定码：旧代码 pause 是 if、missed 是 elif，检测器连续丢 3 拍
        # 以上时设备漏检证据被 pause 整个吞掉。取证到心搏 → 改标 missed_suspect（low）；
        # 取证干净 / 拿不到波形 / suspect_enable=false → pause 照报（level/instant 全部原样）。
        pv = None
        if rr_now >= p["pause_extreme_s"] * 1000.0:
            pv = ("high", f"疑似停搏：RR {rr_now/1000:.2f} s")
        elif rr_now >= p["pause_s"] * 1000.0:
            pv = ("medium", f"长间歇：RR {rr_now/1000:.2f} s")
        if pv is not None:
            # 【收尾-2】间隙取证的 k 网格参考 = 摊平后中位 med（与 bpm 同口径）
            ev, gdbg = self._gap_beat_evidence(snap, rr_now, med_raw, med)
            self._gap_dbg = gdbg
            if ev:
                kh = int(gdbg.get("k_hit") or 0)
                est["missed_suspect"] = (
                    "low",
                    f"疑似漏搏（设备漏检嫌疑，待观察）：RR {rr_now:.0f} ms"
                    f"（间隙内第 {kh} 个期望位置取证到心搏）")
            else:
                est["pause"] = pv
        elif last_rr >= p["miss_factor"] * med_raw:
            # 【B】三指纹判别：E1（RR≈2×）且 (E2 影子峰 或 E3 拍幅值不稳) → 设备漏检嫌疑
            # 【收尾-2】E2 的期望位置也改用摊平后中位（与 pause 分支网格口径统一）
            code, lv, txt = self._miss_code(snap, med_raw, med, last_rr)
            est[code] = (lv, txt)

        seg = rr[-int(p["irregular_n"]):]
        if seg.size >= 8:
            # 倍长间隔（多为上游漏检一拍，已由 missed/pause 报过）不参与不齐判定，否则一次漏检
            # 会被误判成"房颤待查"。钳制基准用**摊平后**序列：用 median(seg) 时窗内翻倍拍与
            # 正常拍各半，1.6×med_seg 高于翻倍拍本身、根本不钳，CV 会被撑到 0.26。
            seg_c = self._flatten(seg)
            cv = float(np.std(seg_c, ddof=1) / max(np.mean(seg_c), 1e-9))
            # 量化噪声地板按**实测 fs** 推导（不写死 31.25）：±1 样本抖动 ≈ 1000/fs ms
            fs_now = float(self.ctx.fs or 31.25) if self.ctx else 31.25
            cv_floor = (1000.0 / max(fs_now, 1e-6)) / max(float(np.mean(seg_c)), 1e-9) * 0.8
            if cv > max(p["irreg_cv"], cv_floor):
                est["irregular"] = ("medium",
                                    f"节律不齐：近 {seg.size} 拍 CV {cv:.3f}（房颤待查，需人工判读）")

        # 【A】质量门唯一收口：不可信时把"低危码"映射成 device_suspect(info)。
        #   永不被挡：pause（含 *_extreme，走 instant 通道）、brady(high)、tachy(high)。
        final: Dict[str, Tuple[str, str]] = {}
        for code, (lv, txt) in est.items():
            c2, l2 = self._gate(code, lv)
            if (c2, l2) != (code, lv):
                txt = self._device_text()
            final[c2] = (l2, txt)

        # 【C】候选下沉：本窗没出现的低速码直接丢弃（"连续窗"语义，
        # 防止跨相位调制段把不相邻的证据偷偷累加到 confirm_windows）
        inst = _split(p["instant_codes"])
        for code in [c for c in self._cand if c not in final and c not in inst]:
            self._cand.pop(code, None)
        fired: set = set()
        for code, (lv, txt) in final.items():
            if self._offer(code, lv, txt, beat_idx):
                fired.add(code)

        flags: Dict[str, str] = {c: v[0] for c, v in final.items()}
        flags = self._merge_sticky(flags, now)
        level = _top_level(flags) if flags else "info"
        label = {
            "tachy": "心动过速", "brady": "心动过缓", "pause": "停搏/长间歇",
            "missed": "疑似漏搏", "missed_suspect": "疑似漏搏（设备漏检嫌疑，待观察）",
            "irregular": "节律不齐",
            # 新码（info）：不进节律卡头条、不蜂鸣（voice.py L75-77 已过滤 info），
            # 只在右侧事件日志里以灰点出现 —— 决策"不进头条但显示"（计划 §2.5）
            "device_suspect": "信号质量不足：设备侧疑点，节律不作判读",
        }
        order = ("pause", "tachy", "brady", "irregular", "missed", "missed_suspect",
                 "device_suspect")
        status = "、".join(label[k] for k in order
                          if k in flags) or "窦性节律（演示判定）"
        # 两档文案（计划 §2.1）：未凑够确认窗的码只在状态前缀"观察中：(n/need 窗)"。
        # flags 本身保持"本窗条件成立即入表"的旧语义（既有用例与前端契约都不变，
        # 减速只作用于**事件**——4 连报是事件流上的，不是 flags 上的）。
        unconf = [k for k in order
                  if k in flags and k not in fired and k not in self._sticky
                  and k != "device_suspect"]        # 设备嫌疑本身就是终态，不做"观察中"
        if unconf:
            k0 = unconf[0]
            c0 = self._cand.get(k0)
            need0 = (1 + int(p["instant_recheck"])) if self._is_instant(k0) \
                else int(p["confirm_windows"])
            prog = "" if c0 is None else f"（{int(c0['n_win'])}/{need0} 窗）"
            status = f"观察中：{label[k0]}{prog}" + ("…" if len(unconf) > 1 else "")
        self._latest = {
            "status": status,
            "level": level,
            "flags": flags,
            "bpm_med": round(bpm, 1),
            "med_bpm_ms": round(med, 1),             # 【B2】bpm 用的中位（摊平后），只读
            "med_raw_ms": round(med_raw, 1),         # 【B2】未摊平中位，只读（回归打印用）
            "rr_last_ms": round(last_rr, 1),
            "n_rr": int(rr.size),
            # 【C】观察进度（只增不改）：key=code，value={"n_win": 已凑窗数, "need": 需要窗数}
            "pending": {k: {"n_win": int(v["n_win"]),
                            "need": int(p["confirm_windows"])}
                        for k, v in self._cand.items()},
            # 【B】三指纹证据（只读，回归/上板判读用）
            "miss_dbg": dict(self._miss_dbg),
            # 【回补2】间隙取证明细（只读，只增字段）：k 位置 / 幅值 / 比值 / 是否命中
            "gap_dbg": dict(self._gap_dbg),
            # 【A】质量门状态（只读）：ok=False 时前端 chip 显示「信号不足」
            "trust": {"ok": bool(self._trust_ok), "why": list(self._trust_why),
                      "n": int(self._untrust_total)},
            "recent": list(self._recent)[-6:],
            "note": "演示级规则判定，非诊断用途",
        }
        self.ctx.publish("arrhythmia", self._latest)

    # ------------------------------------------------------- 【A】质量门
    def _trust(self, snap) -> Tuple[bool, List[str]]:
        """返回 (可信, 原因列表)。**缺数据一律放行**（否则 --plugins arrhythmia
        单启用时会全哑；红线 4：缺数据 ≠ 不可信）。"""
        if not self.params["untrusted_enable"]:
            return True, []
        p = self.params
        ex = getattr(snap, "extra", None) or {}
        why: List[str] = []
        fs_used = float(ex.get("fs_used") or getattr(snap, "fs", 0.0) or 0.0)
        fs_sw = float(ex.get("fs_sw") or 0.0)
        # 33% 时基错配只让 sqi 掉 15%（方案 §1.5），所以 fs 一致性必须是**硬门限**，
        # 不能靠 sqi 间接表达。
        if fs_used > 0.0 and fs_sw > 0.0 and abs(fs_sw / fs_used - 1.0) > float(p["fs_dev_max"]):
            why.append(f"fs_mismatch({fs_sw:.2f}/{fs_used:.2f})")
        q = self.ctx.latest("quality") if self.ctx is not None else None
        if q:
            if float(q.get("sqi") or 1.0) < float(p["sqi_min"]):
                why.append("sqi_low")
            if str(q.get("electrode") or "") == "脱落":
                why.append("lead_off")
        if len(self._ok_hist) >= 6:
            ratio = sum(1 for _t, v in self._ok_hist if v) / float(len(self._ok_hist))
            if ratio < float(p["ok_ratio_min"]):
                why.append(f"hr_ok {ratio:.0%}")
        # 【A-2】回补3：连续漏检占比（报告的 R3）。漏检段卡在 hr_ok≈50% 时前三条都不成立，
        # 于是"一次漏检 → irregular/brady"照样上卡片；这一条专治它。
        mr, _ref = self._miss_ratio()
        if mr > float(p["miss_ratio_max"]):
            why.append(f"miss_ratio {mr:.0%}")
        return (not why), why

    def _miss_ratio(self) -> Tuple[float, float]:
        """近 ≤12 拍里 `rr ≥ miss_factor × 参考中位` 的占比；返回 (占比, 参考中位 ms)。

        参考中位**必须用摊平后口径**（= bpm 用的那个 med）：漏检段会把 med_raw 抬高，
        拿它当参考时"翻倍拍 ≥ 1.6×med_raw"永远不成立，这条维度等于白加。
        样本不足 6 拍返回 0（缺数据放行）。
        """
        p = self.params
        n = len(self._rr)
        if n < 6:
            return 0.0, 0.0
        arr = np.asarray(self._rr, dtype=np.float64)
        ref = float(np.median(self._flatten(arr[-8:])))
        if ref <= 0.0:
            return 0.0, 0.0
        seg = arr[-min(12, n):]
        if seg.size < 6:
            return 0.0, ref
        return float(np.mean(seg >= float(p["miss_factor"]) * ref)), ref

    def _update_trust(self, snap) -> None:
        """质量门状态更新（每次评估一次）：防抖 + 重标定清零。"""
        ex = getattr(snap, "extra", None) or {}
        rc = int(ex.get("recal_count") or 0)
        if rc != self._recal_seen:
            # P1 在线重标定瞬间 fs_sw 会跳变（方案 §三 A 风险项）：清零计时与 hr_ok 历史，
            # 否则刚重标定的头几秒会被误判成"设备嫌疑"，把演示格打成全哑。
            self._recal_seen = rc
            self._untrust_n = 0
            self._trust_ok = True
            self._trust_why = []
            self._ok_hist.clear()
        ok, why = self._trust(snap)
        if ok:
            self._untrust_n = 0
            self._trust_ok = True
            self._trust_why = []
            return
        self._untrust_n += 1
        self._untrust_total += 1
        if self._untrust_n >= 2:            # 防抖：连续 2 次评估（≈1 s）才生效（硬编码）
            self._trust_ok = False
            self._trust_why = list(why)

    def _device_text(self) -> str:
        why = self._trust_why or ["证据不足"]
        return "信号质量不足：设备侧疑点，节律不作判读（" + " · ".join(why) + "）"

    def _gate(self, code: str, level: str) -> Tuple[str, str]:
        """唯一收口：不可信时把"低危码"映射成 device_suspect(info)。"""
        if not self.params["untrusted_enable"]:
            return code, level
        if self._is_instant(code):          # pause / *_extreme 永不被门挡（红线 1/3）
            return code, level
        if level == "high":                 # 极端区照报（brady(high) / tachy(high)）
            return code, level
        if not self._trust_ok:
            return "device_suspect", "info"
        return code, level

    # --------------------------------------------------- 【B】漏检三指纹
    def _amp_at(self, snap, i_lo: int, i_hi: int) -> Optional[float]:
        """返回 [i_lo, i_hi) 相对该段基线的最大偏离（码值）。段太短/越界返回 None。

        下标换算与 `webmon.py::_wave_index` 同法：绝对样本号 → wave 数组下标。
        """
        w = getattr(snap, "wave", None)
        if w is None or len(w) < 8:
            return None
        ws = int(getattr(snap, "n_total", 0)) - int(len(w))
        a, b = int(i_lo) - ws, int(i_hi) - ws
        a, b = max(0, a), min(len(w), b)
        if b - a < 2:
            return None
        seg = np.asarray(w[a:b], dtype=np.float64)
        return float(np.max(np.abs(seg - np.median(seg))))

    def _probe_win(self, snap, center: float, i_lo: int, i_hi: int,
                   med_raw: float, fs: float) -> Optional[Tuple[int, int]]:
        """以「期望漏拍位置」为中心的取证窗（**E2 与 pause 间隙取证共用同一口径**）。

        窗 = center ± max(0.12×med_raw, gap_win_half_s)，再钳制到
        (prev_R + max(0.30 s, 0.20×med_raw), next_R − 0.20 s)：下界排除前拍 T 波尾，
        上界避免贴到 next_R 本尊。有效样本 <2 / 越界 → None，调用方按"缺数据"处理。
        """
        w = getattr(snap, "wave", None)
        if w is None or len(w) < 8 or fs <= 0.0 or med_raw <= 0.0:
            return None
        med_n = float(med_raw) * fs / 1000.0
        if med_n <= 1.0:
            return None
        half = max(0.12 * med_n, float(self.params["gap_win_half_s"]) * fs)
        lo_lim = float(i_lo) + max(0.30 * fs, 0.20 * med_n)
        hi_lim = float(i_hi) - 0.20 * fs
        if hi_lim - lo_lim < 2.0:
            return None
        a = int(round(max(float(center) - half, lo_lim)))
        b = int(round(min(float(center) + half, hi_lim)))
        if b - a < 2:
            return None
        ws = int(getattr(snap, "n_total", 0)) - int(len(w))
        if min(len(w), b - ws) - max(0, a - ws) < 2:      # 与 _amp_at 同法的越界裁剪
            return None
        return a, b

    def _shadow_evidence(self, snap, idx: List[int], fs: float,
                         med_raw: float,
                         med_ref: Optional[float] = None) -> Tuple[bool, bool, Dict[str, Any]]:
        """E2（影子峰，期望位置定位窗）/ E3（拍幅值 cv）。缺数据一律返回 False（按真漏搏报）。

        返回 (e2, e3, dbg)。期望位置 pos = prev_R + med_ref（摊平后中位，缺省回退 med_raw），
        与 pause 分支的 k 网格同口径；窗宽与钳制区仍按 med_raw 算。
        """
        p = self.params
        dbg: Dict[str, Any] = {}
        if len(idx) < 2 or fs <= 0.0:
            return False, False, dbg
        nb = max(2, int(round(float(p["amp_nbhd_sec"]) * fs)))
        i_lo, i_hi = int(idx[-2]), int(idx[-1])
        amp_prev = self._amp_at(snap, i_lo - nb, i_lo + nb + 1)
        amp_next = self._amp_at(snap, i_hi - nb, i_hi + nb + 1)
        # ---- E2：漏拍的**期望位置** pos = prev_R + med_ref（【收尾-2】口径：摊平后中位，
        #      与 pause 分支的 k 网格统一；E1 已证 ratio≈2 时漏拍在正中），
        #      再用 _probe_win 的定位窗 + 双向钳制取证。参考幅值仍是 min(前后拍邻域)。
        e2 = False
        if amp_prev is not None and amp_next is not None:
            # 期望位置口径 = med_ref（摊平后中位）；窗宽/钳制区仍用 med_raw（不动）
            ref_ms = float(med_raw) if med_ref is None or float(med_ref) <= 0.0 \
                else float(med_ref)
            pos_n = ref_ms * fs / 1000.0
            win = self._probe_win(snap, i_lo + pos_n, i_lo, i_hi, med_raw, fs)
            if win is not None:
                shadow = self._amp_at(snap, win[0], win[1])
                ref = min(float(amp_prev), float(amp_next))
                if shadow is not None and ref > 1e-9:
                    e2 = float(shadow) >= float(p["amp_shadow"]) * ref
                    dbg = {"e2_pos_ms": round(pos_n / fs * 1000.0, 1),
                           "e2_med_ref": round(ref_ms, 1),
                           "e2_win_ms": [round((win[0] - i_lo) / fs * 1000.0, 1),
                                         round((win[1] - i_lo) / fs * 1000.0, 1)],
                           "e2_shadow": round(float(shadow), 1),
                           "e2_ref": round(ref, 1),
                           "e2_ratio": round(float(shadow) / ref, 3)}
        # ---- E3：近 6 拍 R 峰幅值的 cv（采样相位调制的指纹）
        e3 = False
        amps = []
        for i in idx[-6:]:
            v = self._amp_at(snap, int(i) - nb, int(i) + nb + 1)
            if v is not None:
                amps.append(float(v))
        if len(amps) >= 4:
            m = float(np.mean(amps))
            if m > 1e-9:
                e3 = (float(np.std(amps, ddof=1)) / m) >= float(p["amp_cv_suspect"])
        return bool(e2), bool(e3), dbg

    def _miss_code(self, snap, med_raw: float, med_ref: float,
                   last_rr: float) -> Tuple[str, str, str]:
        """漏搏码判定：`E1 且 (E2 或 E3)` → missed_suspect；否则 → missed（真漏搏照报）。

        `med_raw` 仍用于 E1（RR≈2× 指纹，拍板口径不改）；`med_ref`（摊平后中位）只用于
        E2 的期望位置，见 `_shadow_evidence`。
        """
        p = self.params
        m = max(float(med_raw), 1e-9)
        ratio_rr = float(last_rr) / m
        # ---- E1（前置条件）：RR≈2× 指纹。没有它一律按真漏搏——真二度阻滞 2:1
        #      在样本域数出来的倍数同样≈2，把"样本域双计数"当独立指纹会让真漏搏
        #      被误标成设备嫌疑（红线 2），故本轮把它降级为日志证据。
        e1 = (float(p["flatten_band_lo"]) <= ratio_rr < float(p["flatten_band_hi"])
              and abs(float(last_rr) - 2.0 * m) / m < float(p["flatten_tol"]))
        b = beats_of(snap)
        idx = [int(v) for v in (b.get("idx_abs") or ())]
        fs = float(getattr(snap, "fs", 0.0) or 0.0)
        if fs <= 0.0 and self.ctx is not None:
            fs = float(self.ctx.fs or 0.0)
        ratio_sample = 0.0
        if len(idx) >= 2:
            base_bs = float(np.median(np.diff(idx[-6:]))) if len(idx) >= 3 \
                else float(idx[-1] - idx[-2])
            ratio_sample = float(idx[-1] - idx[-2]) / max(base_bs, 1e-9)
        e2, e3, d2 = (False, False, {})
        if bool(p["suspect_enable"]) and e1:
            e2, e3, d2 = self._shadow_evidence(snap, idx, fs, med_raw, med_ref)
        self._miss_dbg = {"ratio_rr": round(ratio_rr, 3),
                          "ratio_sample": round(ratio_sample, 3),
                          "e1": bool(e1), "e2": bool(e2), "e3": bool(e3)}
        self._miss_dbg.update(d2)                    # E2 定位窗取证明细（只增字段）
        if e2 or e3:
            return ("missed_suspect", "low",
                    f"疑似漏搏（设备漏检嫌疑，待观察）：RR {last_rr:.0f} ms")
        return ("missed", "low", f"疑似漏搏：RR {last_rr:.0f} ms（中位 {med_raw:.0f} ms）")

    # ------------------------------------------- 【回补2】pause 间隙取证改标
    def _gap_beat_evidence(self, snap, rr_now: float, med_raw: float,
                           med_ref: Optional[float] = None
                           ) -> Tuple[Optional[bool], Dict[str, Any]]:
        """长间隙（rr_now ≥ pause_s）里取证"心搏是否还在"，用于 pause ↔ missed_suspect 改标。

        返回 (evidence, dbg)：True = 期望位置上取证到心搏（改标 missed_suspect，不报 pause）；
        False = 所有 k 位置都干净 → 真停搏，pause 照报；None = 拿不到波形 / 越界 / 判别关闭
        → 按真停搏报。k 网格参考拍间隔 = 摊平后中位 med_ref（缺省回退 med_raw），k 从 1 起扫、
        cap 4；口径与 E2 一致（_probe_win + amp_shadow × min(邻拍幅值)）。
        """
        p = self.params
        ref_beat = float(med_ref) if (med_ref is not None and float(med_ref) > 0.0) \
            else float(med_raw)
        dbg: Dict[str, Any] = {"gap_ms": round(float(rr_now), 1),
                               "med_raw": round(float(med_raw), 1),
                               "med_ref": round(ref_beat, 1),
                               "ref_beat": round(ref_beat, 1),
                               "k_max": 0, "k_hit": 0, "hits": []}
        if not bool(p["suspect_enable"]):
            dbg["off"] = True
            return None, dbg
        b = beats_of(snap)
        idx = [int(v) for v in (b.get("idx_abs") or ())]
        fs = float(getattr(snap, "fs", 0.0) or 0.0)
        if fs <= 0.0 and self.ctx is not None:
            fs = float(self.ctx.fs or 0.0)
        if fs <= 0.0 or float(med_raw) <= 0.0 or len(idx) < 2:
            dbg["no_data"] = "idx/fs"
            return None, dbg
        # 长间歇到底是哪两拍之间：rr_now = max(rr[-lb:])，在回看窗里定位那一对
        lb = max(1, int(p["instant_lookback_beats"]))
        n = len(idx)
        tail = [float(v) for v in list(self._rr)[-lb:]]
        j = int(np.argmax(tail)) if tail else 0
        j = max(0, min(j, min(lb, n - 1) - 1))
        if n >= lb + 1:
            i_lo, i_hi = int(idx[n - lb - 1 + j]), int(idx[n - lb + j])
        else:
            i_lo, i_hi = int(idx[-2]), int(idx[-1])
        nb = max(2, int(round(float(p["amp_nbhd_sec"]) * fs)))
        amp_prev = self._amp_at(snap, i_lo - nb, i_lo + nb + 1)
        amp_next = self._amp_at(snap, i_hi - nb, i_hi + nb + 1)
        if amp_prev is None or amp_next is None:
            dbg["no_data"] = "nbhd"
            return None, dbg
        ref = min(float(amp_prev), float(amp_next))
        if ref <= 1e-9:
            dbg["no_data"] = "ref"
            return None, dbg
        dbg["amp_ref"] = round(ref, 1)
        med_n = ref_beat * fs / 1000.0        # 网格步长（摊平后中位）；窗宽/钳制区仍按 med_raw
        k_max = int(min(4, np.floor(float(rr_now) / max(ref_beat, 1e-9))))
        dbg["k_max"] = k_max
        if k_max < 1:                          # 【收尾-2】k 从 1 起扫，故门槛由 2 降为 1
            dbg["no_k"] = True
            return None, dbg
        seen = False
        for k in range(1, k_max + 1):
            win = self._probe_win(snap, i_lo + k * med_n, i_lo, i_hi, med_raw, fs)
            if win is None:
                continue
            v = self._amp_at(snap, win[0], win[1])
            if v is None:
                continue
            seen = True
            r = float(v) / ref
            hit = r >= float(p["amp_shadow"])
            dbg["hits"].append({"k": k, "pos_ms": round(k * ref_beat, 1),
                                "amp": round(float(v), 1), "ratio": round(r, 3),
                                "hit": bool(hit)})
            if hit:
                dbg["k_hit"] = k
                return True, dbg
        if not seen:
            dbg["no_data"] = "win"
        return (False if seen else None), dbg

    # ------------------------------------------------------- 【B2】中位摊平
    def _flatten(self, vals: np.ndarray) -> np.ndarray:
        """把"明确是 2 倍"的间隔摊成两段（治假过缓 / 倍长钳制）。

        基准取 flatten_q（默认 0.25 = 25 分位，按比例给）：25 分位仍落在正常拍上，能把
        1242/671 判成 2 倍；35 分位会被插值抬到 928 ms 而判不出倍数。
        只摊 [flatten_band_lo, flatten_band_hi) 且 |v-2b|/b < flatten_tol 的段 ——
        房颤样 1.1 s vs 2×0.62 s 偏差 22% > 15%，因此不会被误摊。
        """
        p = self.params
        if not p["flatten_med_enable"] or vals.size == 0:
            return vals
        q = float(p["flatten_q"])
        base = max(float(np.percentile(vals, q * 100.0 if q <= 1.0 else q)), 200.0)
        out = []
        for v in vals:
            r = float(v) / base
            if (float(p["flatten_band_lo"]) <= r < float(p["flatten_band_hi"])
                    and abs(float(v) - 2.0 * base) / base < float(p["flatten_tol"])):
                out.extend([float(v) / 2.0, float(v) / 2.0])
            else:
                out.append(float(v))
        return np.asarray(out, dtype=np.float64)

    # ------------------------------------------------------------ 【C】减速
    def _is_instant(self, code: str) -> bool:
        """即时通道判定：instant_codes 里的码 + 一切 *_extreme（极端区不减速）。"""
        if code.endswith("_extreme"):
            return True
        return code in _split(self.params["instant_codes"])

    def _offer(self, code: str, level: str, text: str, beat_idx: int) -> bool:
        """候选登记 + 升级（替代"单窗即报"）。返回本次是否真的 _fire 了。

        beat_idx 是**绝对样本号**，不是拍序号：`beat_idx 推进 ≥2` 的语义是"换了一批新拍"。
        若改成真正的"拍计数推进 ≥2"，45~90 bpm 下每次评估只多 1 拍，独立窗永远凑不够。
        """
        now = time.monotonic()
        p = self.params
        instant = self._is_instant(code)
        cand = self._cand.get(code)
        if cand is None:
            # 低速码：本窗本身就是第 1 个证据窗（否则 confirm_windows=1 也要等下一窗，
            # §2.8 的"参数级回滚到单窗即报"就不成立了）；instant 码从 0 起算。
            self._cand[code] = {"first": now, "last": now,
                                "n_win": 0 if instant else 1,
                                "last_beat": beat_idx, "level": level, "text": text}
            cand = self._cand[code]
        if instant:
            cand["n_win"] += 1                       # instant：每次评估都算一次（≈0.5~1 s）
            if cand["n_win"] >= 1 + int(p["instant_recheck"]):
                self._fire(code, level, text)
                cand["n_win"] = 0                    # 冷却期由 _fire 管，这里只清计数
                cand["last"] = now
                return True
            return False
        # 低速码：只有"独立窗"才计数（双条件，见计划 §2.1 口径 1）
        fresh = (now - cand["last"] >= float(p["update_sec"])
                 and beat_idx - cand["last_beat"] >= 2)
        if fresh:
            cand["n_win"] += 1
            cand["last"] = now
            cand["last_beat"] = beat_idx
            cand["level"], cand["text"] = level, text
        need = int(p["confirm_windows"])
        # confirm_windows<=1 时 confirm_span_s 自然失效（回滚语义：单窗即报）
        if cand["n_win"] >= need and (need <= 1
                                      or (now - cand["first"]) >= float(p["confirm_span_s"])):
            self._fire(code, cand["level"], cand["text"])
            self._cand.pop(code, None)
            return True
        return False

    def _cand_tick(self, now: float) -> None:
        """候选过期清理（防"永远挂着观察中"的候选把状态熬成假的持续）。"""
        tmo = float(self.params["cand_timeout_s"])
        for code in [c for c, v in self._cand.items() if now - float(v["first"]) > tmo]:
            self._cand.pop(code, None)

    def _fire(self, code: str, level: str, text: str) -> None:
        now = time.monotonic()
        if now - self._cool.get(code, 0.0) < self.params["cooldown"]:
            return
        self._cool[code] = now
        self._sticky[code] = (level, now + max(4.0, 2.0 * self.params["pause_s"]))
        self._recent.append({"t": time.strftime("%H:%M:%S"), "code": code,
                             "level": level, "text": text,
                             # 只增不改：quick30 把 recent 原样写进报告，人工据此
                             # 区分"已确认 / 待观察"（计划 §2.5，quick30 不改代码）
                             "suspect": code.endswith("_suspect")})
        self.ctx.emit_event(level, text, code)

    def _merge_sticky(self, flags: Dict[str, str], now_mono: float) -> Dict[str, str]:
        """把仍在粘滞期内、当前条件已不成立的标记补回（评审习惯：事件后留痕数秒）。"""
        merged = dict(flags)
        for code in list(self._sticky):
            level, until = self._sticky[code]
            if now_mono > until:
                del self._sticky[code]
                continue
            merged.setdefault(code, level)
        return merged

    def stats(self) -> Dict[str, Any]:
        st = super().stats()
        st.update({"status": self._latest.get("status"), "n_rr": len(self._rr)})
        return st


def _split(spec: Any) -> set:
    """把 "a,b,c" 形式的参数拆成集合（空/None → 空集）。"""
    return {s.strip() for s in str(spec or "").split(",") if s.strip()}


def _top_level(flags: Dict[str, str]) -> str:
    for lv in ("high", "medium", "low"):
        if lv in flags.values():
            return lv
    return "info"
