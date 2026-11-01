"""sync_board_pkg.py —— 把 src/ 的改动同步到上板部署包 board_pkg/（白名单 + 哈希留证）。

源头只写 src/，board_pkg 是部署镜像；只同步白名单内容，绝不整目录镜像（不带 *.bak_* /
__pycache__）。每个文件打印两侧 sha256，不一致即报错。用法：python tools/sync_board_pkg.py。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SRC = ROOT
DST = os.path.abspath(os.path.join(ROOT, "..", "..", "board", "board_pkg"))

TOP_MODULES = ["__init__.py", "acq_loop.py", "checkup.py", "display_link.py",
               "ecg_hr.py", "fake_afe.py", "ps_afe.py", "regs.py", "sim_ecg.py",
               "sim_afe.py"]
EXTRA_ROOT = ["board_demo.py"]                       # src/board_demo.py → board_pkg/board_demo.py
SUB_PREFIXES = ("plugins/", "webmon/")
STATIC_EXT = (".py", ".css", ".js", ".html", ".svg", ".png")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def collect() -> list:
    pairs = []
    for name in TOP_MODULES:
        s = os.path.join(SRC, "ecgb", name)
        if os.path.isfile(s):
            pairs.append((s, os.path.join(DST, "ecgb", name)))
    for name in EXTRA_ROOT:
        s = os.path.join(SRC, name)
        if os.path.isfile(s):
            pairs.append((s, os.path.join(DST, name)))
    for base, _dirs, files in os.walk(os.path.join(SRC, "ecgb")):
        for f in files:
            if not f.endswith(STATIC_EXT) or ".bak" in f:
                continue
            s = os.path.join(base, f)
            rel = os.path.relpath(s, os.path.join(SRC, "ecgb")).replace("\\", "/")
            if rel.startswith(SUB_PREFIXES) and "__pycache__" not in rel:
                pairs.append((s, os.path.join(DST, "ecgb", rel.replace("/", os.sep))))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pairs = collect()
    print(f"[sync] src → board_pkg，共 {len(pairs)} 个文件{'（dry-run）' if args.dry_run else ''}")
    bad = 0
    for s, d in pairs:
        rel = os.path.relpath(s, SRC)
        if args.dry_run:
            print(f"  will copy {rel}")
            continue
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(s, d)
        hs, hd = sha256(s), sha256(d)
        flag = "OK " if hs == hd else "BAD"
        if hs != hd:
            bad += 1
        print(f"  [{flag}] {rel}  {hs[:16]}")
    if bad:
        print(f"[sync] 失败：{bad} 个文件哈希不一致")
        return 1
    print("[sync] 完成。上板前请再跑 checkup，并确认两条口径："
          "① board_pkg 与 src 哈希一致 ② regs.py 未改（改了要先跑 gen_regmap.py）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
