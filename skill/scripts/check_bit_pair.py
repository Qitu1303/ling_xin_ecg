#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_bit_pair.py — PYNQ 交付目录静态自检：bit/hwh 同名同目录配对 + 哈希台账比对。

为什么存在（实测教训）：
  * 相邻多个版本的 bit 字节数可以完全相同（某工程实测六个版本逐字节同长），区分版本只能靠哈希；
  * hwh 常被留在 <工程>.gen/sources_1/bd/<bd>/hw_handoff/ 没复制出来，或复制后没改名同名；
  * "字符串存在 ≠ 版本正确"：hwh 里 grep 到某端口名不代表是那一版（两版都含），只能作结构断言。

用法：
  python check_bit_pair.py <目录1> [<目录2> ...] [--baseline 台账.json] [--hash md5]
                           [--assert INSTANCE=关键字 [--assert ...]]

台账 JSON 格式（键 = 文件名，值为期望属性；缺失的键跳过比对）：
  {
    "<top>_wrapper.bit": {"md5": "<bit 的 md5>", "bytes": <字节数>},
    "<top>_wrapper.hwh": {"md5": "<hwh 的 md5>", "bytes": <字节数>}
  }

退出码：0 = 全部 [OK]/[WARN]；1 = 存在 [FAIL]。只读，不写任何文件。
"""
import argparse
import hashlib
import json
import os
import sys

BIT_EXT, HWH_EXT, XSA_EXT = ".bit", ".hwh", ".xsa"


def out(s):
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("ascii", "replace").decode())


def file_hash(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_dir(d):
    """返回 (bits, hwhs, xsas)：文件名列表。"""
    bits = sorted(f for f in os.listdir(d) if f.lower().endswith(BIT_EXT))
    hwhs = sorted(f for f in os.listdir(d) if f.lower().endswith(HWH_EXT))
    xsas = sorted(f for f in os.listdir(d) if f.lower().endswith(XSA_EXT))
    return bits, hwhs, xsas


def check_dir(d, algo, baseline, asserts, results):
    out("")
    out("=" * 72)
    out("[目录] %s" % d)
    if not os.path.isdir(d):
        out("  [FAIL] 目录不存在或不可访问")
        results.append(False)
        return
    # [1/3] 扫描
    bits, hwhs, xsas = scan_dir(d)
    out("  [1/3] 扫描到 bit=%d hwh=%d xsa=%d" % (len(bits), len(hwhs), len(xsas)))
    if not bits:
        out("  [FAIL] 目录内没有任何 .bit 文件")
        results.append(False)
        return
    hwh_by_stem = {os.path.splitext(f)[0]: f for f in hwhs}
    xsa_by_stem = {os.path.splitext(f)[0]: f for f in xsas}

    # [2/3] 配对 + 哈希
    ok_all = True
    for b in bits:
        stem = os.path.splitext(b)[0]
        bit_path = os.path.join(d, b)
        size = os.path.getsize(bit_path)
        digest = file_hash(bit_path, algo)
        partner = None
        kind = ""
        if stem in hwh_by_stem:
            partner, kind = hwh_by_stem[stem], HWH_EXT
        elif stem in xsa_by_stem:
            partner, kind = xsa_by_stem[stem], XSA_EXT
        if partner is None:
            out("  [FAIL] %s 找不到同名 .hwh/.xsa（hwh 常被留在 <工程>.gen/sources_1/bd/<bd>/hw_handoff/，"
                "复制后必须改成与 bit 同名）" % b)
            ok_all = False
            continue
        p_path = os.path.join(d, partner)
        p_size = os.path.getsize(p_path)
        p_digest = file_hash(p_path, algo)
        out("  [2/3] %s  %d B  %s=%s" % (b, size, algo, digest))
        out("        %s  %d B  %s=%s" % (partner, p_size, algo, p_digest))
        # 台账比对（哈希为主、字节只作截断粗筛）
        for fname, digest_now, size_now in ((b, digest, size), (partner, p_digest, p_size)):
            exp = baseline.get(fname) if baseline else None
            if exp:
                if exp.get(algo) and exp[algo].lower() != digest_now:
                    out("  [FAIL] %s 台账不一致：期望 %s=%s，实测 %s" % (fname, algo, exp[algo], digest_now))
                    ok_all = False
                elif exp.get("bytes") and int(exp["bytes"]) != size_now:
                    out("  [WARN] %s 字节数与台账不符（期望 %s，实测 %s）——检查是否被截断" % (fname, exp["bytes"], size_now))
                else:
                    out("  [OK  ] %s 与台账一致" % fname)
        if kind == XSA_EXT:
            out("  [WARN] %s 走的是 .xsa 退路方案（PYNQ 3.0+ 可解析）；首选仍是同名 .hwh" % partner)
    # [3/3] hwh 结构断言（可选；按 XML 属性 键="值" 计数，跨 shell 引号安全）
    if asserts:
        for hwh in hwhs:
            text = open(os.path.join(d, hwh), "r", encoding="utf-8", errors="replace").read()
            for item in asserts:
                if "=" not in item:
                    out("  [WARN] 断言 %r 缺少 键=值 形式，已跳过" % item)
                    continue
                key, val = item.split("=", 1)
                pat = '%s="%s"' % (key, val)
                cnt = text.count(pat)
                out("  [3/3] %s 结构断言 %s 命中 %d 处" % (hwh, pat, cnt))
                if cnt == 0:
                    out("  [WARN] 断言关键字零命中——注意：字符串断言只能证\"结构完整\"，"
                        "不能证\"版本正确\"（两版可能都含/都不含），版本以哈希台账为准")
    results.append(ok_all)


def main():
    ap = argparse.ArgumentParser(description="bit/.hwh 同名同目录配对 + 哈希台账比对（只读自检）",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("dirs", nargs="+", help="待检查的交付目录（可多个版本目录）")
    ap.add_argument("--baseline", help="哈希台账 JSON（键=文件名，值含 md5/sha256/bytes）")
    ap.add_argument("--hash", default="md5", choices=["md5", "sha256"], help="哈希算法（默认 md5）")
    ap.add_argument("--assert", dest="asserts", action="append", default=[],
                    help="hwh 结构断言，格式 键=值，脚本按 XML 属性 键=\"值\" 计数，可多次。例：--assert SIGIS=INTERRUPT")
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
            out("[0/3] 已加载台账 %s（%d 条）" % (args.baseline, len(baseline)))
        except Exception as e:
            out("[FAIL] 台账读取失败：%s" % e)
            return 1
    else:
        out("[0/3] 未提供台账，仅做配对与指纹输出（建议配合台账使用：bit 定版只认哈希，字节数在相邻版本间可完全相同）")

    out("[1/3] 步骤说明：扫描 → 同名配对+哈希 → （可选）结构断言")
    results = []
    for d in args.dirs:
        try:
            check_dir(d, args.hash, baseline, args.asserts, results)
        except Exception as e:
            out("  [FAIL] 目录检查异常：%r" % e)
            results.append(False)

    out("")
    out("=" * 72)
    out("[判读] 怎么读输出：[OK]=通过；[WARN]=提示（不拦截）；[FAIL]=必须处理（配对缺失/台账不一致）")
    out("[汇总] %d 个目录，失败 %d 个" % (len(results), results.count(False)))
    return 0 if results.count(False) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
