# -*- coding: utf-8 -*-
"""生成 FIR 低通系数（Q1.15，$readmemh 格式，每行 4 位十六进制），写入 data/fir_coef.hex。

指标：fs = 500 Hz（同 tick_gen）、fc = 40 Hz、63 阶 Hamming 窗；
归一化到直流增益 1 后乘 32768 量化到 int16。用法：python gen_fir_coef.py
"""
import math
import os

TAPS = 63
FS = 500.0
FC = 40.0
OUT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "fir_coef.hex"))


def sinc(x):
    if abs(x) < 1e-12:
        return 1.0
    return math.sin(math.pi * x) / (math.pi * x)


def gen():
    M = TAPS - 1
    h = []
    for n in range(TAPS):
        # 理想低通
        lp = 2.0 * (FC / FS) * sinc(2.0 * (FC / FS) * (n - M / 2.0))
        # Hamming 窗
        w = 0.54 - 0.46 * math.cos(2.0 * math.pi * n / M)
        h.append(lp * w)

    # 直流增益归一化: sum(h) = 1
    s = sum(h)
    h = [x / s for x in h]

    # Q1.15 量化
    q = []
    for x in h:
        v = int(round(x * 32768.0))
        v = max(-32768, min(32767, v))
        q.append(v)

    # 修正量化后的直流增益误差: 把残差补到中心抽头 (对线性相位无影响)
    residue = 32768 - sum(q)
    q[M // 2] = max(-32768, min(32767, q[M // 2] + residue))

    # 纯数据行，不加注释——不同仿真器对 $readmemh 文件里的 // 注释处理不一致
    with open(OUT, "w") as f:
        for v in q:
            f.write("%04X\n" % (v & 0xFFFF))

    # 打印校验信息
    dc = sum(q) / 32768.0
    peak = max(abs(v) / 32768.0 for v in q)
    print("已生成 %s (%d 个系数)" % (OUT, TAPS))
    print("  直流增益     = %.6f (理想 1.000000)" % dc)
    print("  最大系数幅度 = %.6f (需 < 1.0)" % peak)
    print("  系数量化范围 = [%d, %d]" % (min(q), max(q)))

    # 频响抽查
    for fq in (0.0, 10.0, 40.0, 50.0, 60.0, 100.0, 250.0):
        wr, wi = 0.0, 0.0
        for n, v in enumerate(q):
            ang = -2.0 * math.pi * fq * n / FS
            wr += (v / 32768.0) * math.cos(ang)
            wi += (v / 32768.0) * math.sin(ang)
        mag = math.hypot(wr, wi)
        print("  |H(%6.1f Hz)| = %.4f  (%.1f dB)" % (fq, mag, 20 * math.log10(mag) if mag > 1e-9 else -999))


if __name__ == "__main__":
    gen()
