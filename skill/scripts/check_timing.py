#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_timing.py — 解析 Vivado timing summary 报告，提取 WNS/TNS/WHS/THS 并按阈值判读。

为什么存在（实测教训）：
  * 只判 WNS 会漏掉 hold 违例：实测某版 WNS=+2.054（"过"）而 WHS=-1.862、36 个失败端点，
    结论句为 "Timing constraints are not met."——setup 全绿照样不能交付；
  * 结论句与数字必须双判：数字全正但结论句 not met（或反之）都要拦下；
  * 表头列名含空格（如 "TNS Failing Endpoints"），按 split() 对齐会错位——按已知列名表动态映射；
  * Intra Clock Table 里有只写名字不带数据的子时钟行（CLKFBIN_1 等），跳过、不可当成 0。

用法：
  python check_timing.py <报告1.rpt> [<报告2.rpt> ...] [--wns-warn 0.5] [--whs-warn 0.1] [--per-clock]

报告来自 `report_timing_summary ... -file <top>_timing_summary_routed.rpt`；只有 routed 报告
（头部 Design State : Routed）才含真实 hold 违例。退出码：0=pass；1=存在 fail；2=解析失败。
"""
import argparse
import re
import sys

# Design Timing Summary / Intra Clock Table 共用的已知列（按报告原文顺序）
CANON_COLS = ["WNS(ns)", "TNS(ns)", "TNS Failing Endpoints", "TNS Total Endpoints",
              "WHS(ns)", "THS(ns)", "THS Failing Endpoints", "THS Total Endpoints",
              "WPWS(ns)", "TPWS(ns)", "TPWS Failing Endpoints", "TPWS Total Endpoints"]
META_KEYS = ["Tool Version", "Date", "Host", "Design", "Device", "Speed File", "Design State"]


def out(s):
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("ascii", "replace").decode())


def parse_meta(lines):
    meta = {}
    pat = re.compile(r"^\|\s*([A-Za-z][A-Za-z ]*?)\s*:\s*(.+?)\s*$")
    for ln in lines[:40]:
        m = pat.match(ln)
        if m and m.group(1).strip() in META_KEYS:
            meta[m.group(1).strip()] = m.group(2).strip()
    return meta


def find_section(lines, title):
    """返回 '| <title>' 行的下标；找不到返回 -1。"""
    for i, ln in enumerate(lines):
        if ln.strip() == "| " + title:
            return i
    return -1


def header_cols(line):
    """确认表头含全部已知列；命中返回列名列表（固定顺序），否则 None。"""
    for c in CANON_COLS:
        if c not in line:
            return None
    return list(CANON_COLS)


def parse_summary_row(lines, sec_idx):
    """解析 Design Timing Summary 全局行：返回 {列名: 值} 或 None。"""
    cols = None
    for ln in lines[sec_idx + 1: sec_idx + 12]:
        if cols is None:
            if "WNS(ns)" in ln:
                cols = header_cols(ln)
            continue
        s = ln.strip()
        if not s or set(s) <= set("- "):
            continue
        vals = s.split()
        if len(vals) == len(cols):          # 全局行没有首列时钟名，正好 12 个值
            return dict(zip(cols, vals))
        return None
    return None


def parse_intra_clock(lines, sec_idx):
    """解析 Intra Clock Table：返回 [(时钟名, {列名: 值}), ...]（跳过无数据行与子时钟占位行）。"""
    rows = []
    cols = None
    for ln in lines[sec_idx + 1: sec_idx + 80]:
        s = ln.rstrip()
        if cols is None:
            if "WNS(ns)" in s:
                cols = header_cols(s)
            continue
        if not s.strip() or s.strip().startswith("|"):
            break                            # 表格结束（下一节开始）
        body = s.strip()
        if set(body) <= set("- "):
            continue
        toks = body.split()
        if len(toks) != len(cols) + 1:       # 首列是时钟名；子时钟占位行（只有名字）在此被跳过
            continue
        rows.append((toks[0], dict(zip(cols, toks[1:]))))
    return rows


def parse_clock_summary(lines, sec_idx):
    """解析 Clock Summary：返回 [(名字, 周期ns, 频率MHz), ...]。"""
    rows = []
    started = False
    for ln in lines[sec_idx + 1: sec_idx + 80]:
        if "Period(ns)" in ln:
            started = True
            continue
        if started:
            if not ln.strip() or ln.strip().startswith("|"):
                if not ln.strip():
                    break
                continue
            m = re.match(r"^\s*(\S+)\s+\{[^}]*\}\s+([\d.]+)\s+([\d.]+)\s*$", ln)
            if m:
                rows.append((m.group(1), float(m.group(2)), float(m.group(3))))
    return rows


def verdict(name, val, warn_margin):
    """单个余量指标三档判读。"""
    v = float(val)
    if v <= 0:
        return "[FAIL] %s=%s <= 0" % (name, val)
    if v <= warn_margin:
        return "[WARN] %s=%s 余量偏小（<= %.2f ns）" % (name, val, warn_margin)
    return "[OK  ] %s=%s" % (name, val)


def check_report(path, wns_warn, whs_warn, per_clock):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception as e:
        out("[FAIL] 报告读取失败：%r" % e)
        return 2
    out("")
    out("=" * 72)
    out("[报告] %s" % path)
    meta = parse_meta(lines)
    for k in META_KEYS:
        if k in meta:
            out("  %-13s: %s" % (k, meta[k]))
    if meta.get("Design State") != "Routed":
        out("  [WARN] Design State != Routed——非布线后报告不含真实 hold 违例，判读无意义")
        return 2

    sec = find_section(lines, "Design Timing Summary")
    if sec < 0:
        out("  [FAIL] 未找到 | Design Timing Summary 段")
        return 2
    d = parse_summary_row(lines, sec)
    if d is None:
        out("  [FAIL] Design Timing Summary 数据行解析失败（列数与已知列名表不匹配，"
            "报告可能来自异常版本，请人工查看该段原文）")
        return 2
    out("  [1/3] 全局指标：WNS=%s  TNS=%s(端点 %s/%s)  WHS=%s  THS=%s(端点 %s/%s)" %
        (d["WNS(ns)"], d["TNS(ns)"], d["TNS Failing Endpoints"], d["TNS Total Endpoints"],
         d["WHS(ns)"], d["THS(ns)"], d["THS Failing Endpoints"], d["THS Total Endpoints"]))

    # 结论句（双判之一）
    text = "\n".join(lines)
    if "All user specified timing constraints are met" in text:
        concl, concl_line = "met", "All user specified timing constraints are met."
    elif "Timing constraints are not met" in text:
        concl, concl_line = "not_met", "Timing constraints are not met."
    else:
        concl, concl_line = None, "(未找到结论句)"
    out("  [2/3] 结论句：%s" % concl_line)

    fails = []
    out("  [3/3] 阈值判读：")
    out("        " + verdict("WNS", d["WNS(ns)"], wns_warn))
    out("        " + verdict("WHS", d["WHS(ns)"], whs_warn))
    if float(d["WNS(ns)"]) <= 0:
        fails.append("WNS<=0")
    if float(d["WHS(ns)"]) <= 0:
        fails.append("WHS<=0")
    if int(d["TNS Failing Endpoints"]) > 0:
        out("        [WARN] TNS 失败端点 %s 个——逐条分类再下结论：集中在同一结构=结构性偏斜可评估；散布多时钟域=真问题"
            % d["TNS Failing Endpoints"])
    if int(d["THS Failing Endpoints"]) > 0:
        out("        [WARN] THS 失败端点 %s 个" % d["THS Failing Endpoints"])
        fails.append("THS端点>0")
    # 双判：结论句与数字互相印证
    if concl == "met" and fails:
        out("        [FAIL] 结论句为 met 但指标存在违例（%s）——以数字为准，报告/版本可能错配" % ",".join(fails))
        fails.append("双判不一致")
    elif concl == "not_met":
        out("        [FAIL] 结论句为 not met——无论数字如何，不得放行（只判 WNS 会漏 hold，本行即为此设）")
        fails.append("结论句not_met")
    elif concl is None:
        out("        [WARN] 未找到结论句——按数字判读，但建议人工复核报告完整性")

    if per_clock:
        cs = find_section(lines, "Clock Summary")
        if cs >= 0:
            rows = parse_clock_summary(lines, cs)
            if rows:
                out("  [per-clock] Clock Summary（%d 个）：Period / Freq" % len(rows))
                for name, period, freq in rows:
                    out("      %-42s %8.3f ns  %8.3f MHz" % (name, period, freq))
        ic = find_section(lines, "Intra Clock Table")
        if ic >= 0:
            rows = parse_intra_clock(lines, ic)
            if rows:
                out("  [per-clock] Intra Clock Table（按域 WNS/WHS，标 * 的为 WNS 最差域）：")
                worst = min(rows, key=lambda t: float(t[1]["WNS(ns)"]))
                for name, dd in rows:
                    star = "*" if name == worst[0] else " "
                    out("      %s%-40s WNS=%8.3f  WHS=%8.3f" %
                        (star, name, float(dd["WNS(ns)"]), float(dd["WHS(ns)"])))

    out("  [判定] %s" % ("FAIL（%s）" % ",".join(sorted(set(fails))) if fails else "PASS"))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description="解析 Vivado timing summary：WNS/WHS 阈值判读 + 结论句双判",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("reports", nargs="+", help="*_timing_summary_routed.rpt（可多个）")
    ap.add_argument("--wns-warn", type=float, default=0.5, help="WNS 余量告警线 ns（默认 0.5）")
    ap.add_argument("--whs-warn", type=float, default=0.1, help="WHS 余量告警线 ns（默认 0.1）")
    ap.add_argument("--per-clock", action="store_true", help="附各时钟域 Period/WNS/WHS（定位最差域）")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    out("[0/3] 步骤：头部元数据（确认 Design State=Routed）→ 全局指标 → 结论句 → 阈值判读（可选按域细分）")
    codes = []
    for r in args.reports:
        codes.append(check_report(r, args.wns_warn, args.whs_warn, args.per_clock))
    out("")
    out("=" * 72)
    out("[判读] 怎么读输出：[OK]=通过；[WARN]=余量偏小/有待分类端点（人工评估）；[FAIL]=不得交付；")
    out("       hold 违例逐条分类再下结论：全在同一结构=结构性时钟偏斜（可评估接受）；散布多时钟域=真问题；")
    out("       版本留证：bit 只按 MD5 定版（相邻版本字节数可完全相同），时序报告头部 Date/Design 用于配对版本。")
    out("[汇总] %d 份报告，fail/解析失败 %d 份" % (len(codes), sum(1 for c in codes if c != 0)))
    return 0 if all(c == 0 for c in codes) else (1 if any(c == 1 for c in codes) else 2)


if __name__ == "__main__":
    sys.exit(main())
