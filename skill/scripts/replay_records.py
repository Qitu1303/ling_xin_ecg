#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""replay_records.py — 板上记录（session 目录）拉回后的离线完整性复核 + 黄金基线比对。

为什么存在（实测教训）：
  * 板上 recorder 的分片可能中途开始（n 起点可 >1），不能假设样本号从 1 起；
  * t_s 是合成时基（n/fs），不是墙钟；板上系统时钟可能停在镜像发布日——文件时间戳/session 名不可信；
  * "离线绿 ≠ 板上行为一致"：拉回数据先做完整性/连续性复核，再与黄金基线比口径，别直接信"看着没问题"。

会话目录约定：records/session_*/ 内含 part01.csv...（列 n,code,volt,t_s）与 summary*.json（可缺，
文件名可能带空格后缀如 "summary (1).json"，按 summary*.json 通配）。

用法：
  python replay_records.py <会话目录> [<会话目录> ...] [--fs F] [--tol 0.05] [--baseline 基线.json] [--align-check]

基线 JSON 格式：{"fs": <离线回归基线 fs>, "n_written": <期望样本数>}（缺项跳过该比对）。
退出码：0 = 全部 [OK]/[WARN]；1 = 存在 [FAIL]（不连续/基线越界/无法定 fs）。
"""
import argparse
import csv
import glob
import json
import os
import sys


def out(s):
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("ascii", "replace").decode())


def read_csv(path):
    """返回 (rows, header)。rows 为 dict 列表；列名按表头动态取。"""
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        r = csv.DictReader(f)
        header = [h.strip() for h in (r.fieldnames or [])]
        rows = []
        for row in r:
            rows.append({k.strip(): v for k, v in row.items() if k is not None})
    return rows, header


def check_session(session_dir, fs_opt, tol, baseline, align_check):
    ok = True
    out("")
    out("=" * 72)
    out("[会话] %s" % session_dir)
    if not os.path.isdir(session_dir):
        out("  [FAIL] 目录不存在")
        return False

    # [1/4] summary 定位（文件名不规范：可能是 "summary (1).json"，按通配找）
    summaries = sorted(glob.glob(os.path.join(session_dir, "summary*.json")))
    fs = fs_opt
    if summaries:
        try:
            with open(summaries[0], "r", encoding="utf-8") as f:
                meta = json.load(f)
            fs = fs or meta.get("fs")
            out("  [1/4] summary=%s  fs=%s  n_written=%s  duration=%s" %
                (os.path.basename(summaries[0]), meta.get("fs"), meta.get("n_written"), meta.get("duration_s")))
            if meta.get("reason"):
                out("        收尾原因=%s  起止墙钟=%s ~ %s（板上时钟可能不可信，仅供索引）"
                    % (meta.get("reason"), meta.get("started_wall"), meta.get("ended_wall")))
        except Exception as e:
            out("  [WARN] summary 读取失败：%r" % e)
    else:
        out("  [1/4] 未找到 summary*.json（分片可能未正常收尾）")
    if fs is None:
        out("  [INFO] 无 summary 且未给 --fs，稍后按时基步长反解（仅参考口径）")

    # [2/4] 分片清点
    parts = sorted(glob.glob(os.path.join(session_dir, "part*.csv")))
    out("  [2/4] 分片 %d 个：%s" % (len(parts), ", ".join(os.path.basename(p) for p in parts) or "(无)"))
    if not parts:
        out("  [FAIL] 没有任何 part*.csv")
        return False

    # [3/4] 逐分片复核
    total = 0
    prev_n_end, prev_t_end, prev_part = None, None, None
    fs_estimates = []
    for p in parts:
        try:
            rows, header = read_csv(p)
        except Exception as e:
            out("  [FAIL] %s 读取失败：%r" % (p, e))
            return False
        if not rows:
            out("  [FAIL] %s 无数据行" % os.path.basename(p))
            return False
        if not all(c in header for c in ("n", "code", "t_s")):
            out("  [FAIL] %s 缺少必备列 n/code/t_s（实际表头：%s）" % (os.path.basename(p), header))
            return False
        ns = [int(float(r["n"])) for r in rows]
        ts = [float(r["t_s"]) for r in rows]
        codes = [int(float(r["code"])) for r in rows]
        n_start, n_end = ns[0], ns[-1]
        # 分片内：严格递增、无重复
        mono = all(b > a for a, b in zip(ns, ns[1:]))
        # 分片间：n 连续（允许中途起：下一片首行 = 上一片末行 + 1）
        link = "首片"
        if prev_n_end is not None:
            link = "连续" if n_start == prev_n_end + 1 else "断链(前片末 n=%d)" % prev_n_end
        # 时基步长
        steps = [b - a for a, b in zip(ts, ts[1:])]
        step_med = sorted(steps)[len(steps) // 2] if steps else 0.0
        if step_med > 0:
            fs_estimates.append(1.0 / step_med)
        out("  [3/4] %s  行数=%d  n=[%d..%d]  %s  t_s=[%.4f..%.4f]  步长中位=%.4fs"
            % (os.path.basename(p), len(rows), n_start, n_end,
               ("单调递增[OK]" if mono else "非单调[FAIL]"), ts[0], ts[-1], step_med))
        if align_check:
            rem = sum(1 for c in codes if c % 16 != 0)
            out("        码值非 16 倍数计数=%d（提示：该判据只对 XADC 原始码值路径有意义——原始码 12-bit "
                "左对齐应为 16 的倍数；滤波后/软件侧数值通常不是，非零不判失败）" % rem)
        if not mono:
            ok = False
        if "断链" in link:
            out("        [FAIL] 与前片 %s 不连续：%s" % (prev_part, link))
            ok = False
        if codes:
            out("        码值 min=%d max=%d p2p=%d" % (min(codes), max(codes), max(codes) - min(codes)))
        prev_n_end, prev_t_end, prev_part = n_end, ts[-1], os.path.basename(p)
        total += len(rows)

    # [4/4] fs 口径与基线比对
    fs_summary, fs_step = fs, (sorted(fs_estimates)[len(fs_estimates) // 2] if fs_estimates else None)
    if fs_summary is None:
        fs_summary = fs_step
        if fs_step is None:
            out("  [FAIL] 无法确定 fs（无 summary、无 --fs、时基步长为 0）")
            return False
        out("  [4/4] fs 按时基步长反解 = %.4f（仅参考口径：t_s 是合成时基 n/fs，不是墙钟）" % fs_step)
    else:
        out("  [4/4] fs（summary 口径）= %.4f" % fs_summary)
        if fs_step is not None and abs(fs_step - fs_summary) > max(0.02, tol * fs_summary):
            out("        [WARN] 步长反解 fs=%.4f 与 summary 口径偏差 > %.0f%%——检查投递率缺口或分片拼接"
                % (fs_step, tol * 100))
    if baseline:
        if baseline.get("fs"):
            dev = abs(fs_summary - baseline["fs"]) / baseline["fs"]
            out("        基线 fs=%.4f 偏差=%.2f%%（容差 %.0f%%）%s"
                % (baseline["fs"], dev * 100, tol * 100, "[OK]" if dev <= tol else "[FAIL]"))
            if dev > tol:
                ok = False
        if baseline.get("n_written"):
            dev = abs(total - int(baseline["n_written"])) / baseline["n_written"]
            out("        基线 n_written=%s 实测=%d 偏差=%.2f%% %s"
                % (baseline["n_written"], total, dev * 100, "[OK]" if dev <= tol else "[FAIL]"))
            if dev > tol:
                ok = False
    else:
        out("        （未给 --baseline，跳过黄金参考比对；建议按离线回归基线建一份）")
    return ok


def main():
    ap = argparse.ArgumentParser(description="板上记录离线复核：完整性/连续性/时基口径 + 黄金基线比对（只读）",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("sessions", nargs="+", help="会话目录（含 part*.csv 与 summary*.json）")
    ap.add_argument("--fs", type=float, default=None, help="指定 fs，覆盖 summary 口径")
    ap.add_argument("--tol", type=float, default=0.05, help="基线相对容差（默认 0.05 = 5%%）")
    ap.add_argument("--baseline", help="黄金基线 JSON：{\"fs\":..,\"n_written\":..}")
    ap.add_argument("--align-check", action="store_true", help="码值 16 倍数检查（12-bit 左对齐口径）")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    baseline = {}
    if args.baseline:
        try:
            with open(args.baseline, "r", encoding="utf-8") as f:
                baseline = json.load(f)
            out("[0/4] 已加载基线 %s" % args.baseline)
        except Exception as e:
            out("[FAIL] 基线读取失败：%s" % e)
            return 1
    out("[0/4] 步骤：summary 定位 → 分片清点 → 逐片复核（单调/连续/时基/码值）→ fs 口径与基线比对")
    results = []
    for s in args.sessions:
        try:
            results.append(check_session(s, args.fs, args.tol, baseline, args.align_check))
        except Exception as e:
            out("  [FAIL] 会话检查异常：%r" % e)
            results.append(False)
    out("")
    out("=" * 72)
    out("[判读] 怎么读输出：分片行给 n 起止与连续性；断链/非单调/基线越界为 [FAIL]；")
    out("       n 起点可 >1（中途分片，正常）；t_s 步长 = 1/fs（合成时基，不是墙钟，别当真实时间用）；")
    out("       与板上日志对照时比\"事件码多重集+卡片簇\"，不比条数（帧率不同条数会差一倍）。")
    out("[汇总] %d 个会话，失败 %d 个" % (len(results), results.count(False)))
    return 0 if results.count(False) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
