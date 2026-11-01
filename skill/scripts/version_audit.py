#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""version_audit.py — 源树 vs 部署包逐文件哈希审计（只读，不写盘）。

为什么存在（实测教训）：
  * 手工挑文件同步双树是"包内副本不一致"事故的根源；合并 ≠ 验证；
  * 只比"copy 没报错"不可靠：copy2 之后必须重算两侧哈希；
  * 板上核对还要补三个证据：__file__ 路径、特征串、目录名逐字符比对（内容哈希查不出目录名拼错）。

用法：
  python version_audit.py --src <源树> --dst <部署包> [--include 通配 ...] [--exclude 通配 ...]
                          [--algo sha256] [--check-extra]

默认排除：__pycache__/*、*.pyc、*.bak*、.git/*。
--include 不给 = 源树下全部文件（受管清单即"全部减排除项"）。
退出码：0 = 无差异；1 = 存在缺失/内容不同/多余。
"""
import argparse
import fnmatch
import hashlib
import os
import sys

DEFAULT_EXCLUDE = ["__pycache__/*", "*.pyc", "*.bak*", "*.bak/*", ".git/*", "*/.git/*"]


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


def excluded(rel, patterns):
    pos = rel.replace(os.sep, "/")
    return any(fnmatch.fnmatch(pos, p) or fnmatch.fnmatch(pos, "*/" + p) for p in patterns)


def included(rel, patterns):
    if not patterns:
        return True
    pos = rel.replace(os.sep, "/")
    return any(fnmatch.fnmatch(pos, p) for p in patterns)


def collect(src, includes, excludes):
    files = []
    for root, dirs, names in os.walk(src):
        for n in sorted(names):
            full = os.path.join(root, n)
            rel = os.path.relpath(full, src)
            if excluded(rel, excludes):
                continue
            if not included(rel, includes):
                continue
            files.append(rel.replace(os.sep, "/"))
    return files


def main():
    ap = argparse.ArgumentParser(description="源树 vs 部署包逐文件哈希审计（只读）",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("--src", required=True, help="可维护源头目录")
    ap.add_argument("--dst", required=True, help="部署包目录")
    ap.add_argument("--include", action="append", default=[], help="受管通配（相对 src，可用 */），可多次")
    ap.add_argument("--exclude", action="append", default=[], help="额外排除通配，可多次（叠加默认排除）")
    ap.add_argument("--algo", default="sha256", choices=["sha256", "md5"], help="哈希算法（默认 sha256）")
    ap.add_argument("--check-extra", action="store_true", help="同时报告部署包中不在受管清单内的文件（默认仅提示不判失败）")
    ap.add_argument("--strict-extra", action="store_true", help="EXTRA 也计入失败（配合 --check-extra）")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    src, dst = os.path.abspath(args.src), os.path.abspath(args.dst)
    if not os.path.isdir(src):
        out("[FAIL] 源树不存在：%s" % src)
        return 1
    if not os.path.isdir(dst):
        out("[FAIL] 部署包不存在：%s" % dst)
        return 1
    excludes = DEFAULT_EXCLUDE + args.exclude
    managed = collect(src, args.include, excludes)
    out("[sync 审计] src=%s -> dst=%s，共 %d 个受管文件（算法 %s，只读不写）" % (src, dst, len(managed), args.algo))
    if args.include:
        out("  受管通配：%s" % ", ".join(args.include))
    if not managed:
        out("[FAIL] 受管清单为空——检查 --include/--exclude 是否把全部文件排除了")
        return 1

    missing, diff, ok = [], [], 0
    out("-" * 72)
    for rel in managed:
        s_path = os.path.join(src, *rel.split("/"))
        d_path = os.path.join(dst, *rel.split("/"))
        if not os.path.isfile(d_path):
            out("  [MISS] %-50s 部署包缺失" % rel)
            missing.append(rel)
            continue
        hs = file_hash(s_path, args.algo)
        hd = file_hash(d_path, args.algo)  # copy 之后必须重算，不能只信 copy 没报错
        if hs != hd:
            out("  [BAD ] %-50s 内容不同  src=%s dst=%s" % (rel, hs[:16], hd[:16]))
            diff.append(rel)
        else:
            out("  [OK ] %-50s %s" % (rel, hs[:16]))
            ok += 1
    extra = []
    if args.check_extra:
        dst_files = collect(dst, args.include, excludes)
        managed_set = set(managed)
        extra = [f for f in dst_files if f not in managed_set]
        for f in sorted(extra):
            out("  [EXTRA] %-48s 部署包多余（不在受管清单，默认仅提示；--strict-extra 才判失败）" % f)

    out("-" * 72)
    out("[判读] 怎么读输出：[OK]=一致；[MISS]=部署包缺文件；[BAD]=内容不同；[EXTRA]=部署包多出受管范围外的文件（多为合法的文档/生成物，人工确认即可）")
    out("[汇总] 一致 %d / 缺失 %d / 不同 %d / 多余 %d" % (ok, len(missing), len(diff), len(extra)))
    out("[提醒] ① 本脚本只证\"源树<->部署包\"一致，不证\"板上真是这一版\"——上板后按 T3 模板跑核对格；"
        "② 语义门：改过\"触发下游再生成的文件\"（如寄存器映射源）必须重跑生成器，哈希一致不豁免")
    fail = bool(missing or diff or (extra and args.strict_extra))
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
