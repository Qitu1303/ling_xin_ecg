"""生成 stimulus.hex / golden.hex，并与 RTL 的 output.hex 逐行比对。

stimulus.hex -> 灌给 xadc_wiz_0_sim.v；golden.hex -> Python 参考实现输出；
output.hex -> RTL 仿真实际输出。三段 RTL 都是「进一个样本出一个样本」，
valid 域 1:1 对应，故 golden[i] 与 output.hex 第 i 行同源，可直接逐行比对。
用法：python gen_stimulus.py [cmp [N]]
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
STIM_FILE = os.path.join(DATA, "stimulus.hex")
COEF_FILE = os.path.join(DATA, "fir_coef.hex")
GOLDEN_FILE = os.path.join(DATA, "golden.hex")
OUTPUT_FILE = os.path.join(HERE, "results", "output.hex")

N = 2048          # 样本数，与 xadc_wiz_0_sim 的 STIM_N 一致
FS = 500.0
if len(sys.argv) > 2 and sys.argv[1] == "cmp":
    N = int(sys.argv[2])
ALPHA_Q15 = 32564          # 0.5 Hz 高通，同 hp_05hz.v
NOTCH_DLY = 5              # 50 Hz 陷波，同 notch_50hz.v


def read_coef():
    """读同一份 Q1.15 系数字典表，返回有符号整数列表"""
    coef = []
    with open(COEF_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            v = int(line, 16)
            coef.append(v - 65536 if v >= 32768 else v)
    return coef


def gen_stimulus():
    xs = []
    for n in range(N):
        t = n / FS
        v = (32768.0
             + 10000.0 * math.sin(2 * math.pi * 0.8 * t)    # 有用信号
             + 2000.0 * math.sin(2 * math.pi * 0.15 * t)    # 基线漂移
             + 8000.0 * math.sin(2 * math.pi * 50.0 * t))   # 50 Hz 工频
        v = max(0.0, min(65535.0, v))
        xs.append(int(v))
    with open(STIM_FILE, "w") as f:
        for v in xs:
            f.write("%04X\n" % (v & 0xFFFF))
    return xs


def fl(x128):
    """算术右移 -> 向下取整（对齐 Verilog 的 >>>）"""
    return x128 >> 15


def int18(v):
    """对齐 IW=18 有符号寄存器的回绕行为"""
    v &= 0x3FFFF
    return v - (1 << 18) if v >= (1 << 17) else v


def golden(xs, coef):
    # 1) 无符号 -> 有符号
    x = [v - 32768 for v in xs]
    taps = len(coef)

    # 2) 0.5 Hz 一阶高通: y[n] = floor(a*(y[n-1] + x[n] - x[n-1]) / 2^15)
    y1, yd, xd = [], 0, 0
    for xn in x:
        yd = int18(fl(ALPHA_Q15 * (yd + xn - xd)))
        y1.append(yd)
        xd = xn

    # 3) 梳状陷波: y[n] = (x[n] + x[n-DLY]) >> 1 ，延迟线初值 0
    y2 = []
    for i, xn in enumerate(y1):
        prev = y1[i - NOTCH_DLY] if i >= NOTCH_DLY else 0
        y2.append((xn + prev) >> 1)

    # 4) 63 阶 FIR: y[n] = sum h[k]*x[n-k] >> 15
    y3 = []
    for i in range(len(y2)):
        acc = 0
        for k in range(taps):
            j = i - k
            if j >= 0:
                acc += coef[k] * y2[j]
        y3.append(acc >> 15)

    # 5) 有符号 -> 无符号，先饱和再截位（同 filter_chain.ysat）
    out = []
    for v in y3:
        v += 32768
        v = 0 if v < 0 else (65535 if v > 65535 else v)
        out.append(v)
    return out


def compare(gold):
    try:
        rtl = [int(l.strip(), 16) for l in open(OUTPUT_FILE) if l.strip()]
    except FileNotFoundError:
        print("找不到 %s，请先跑 RTL 仿真" % OUTPUT_FILE)
        return
    m = min(len(gold), len(rtl))
    if not m:
        print("无输出可比对"); return
    errs = [abs(gold[i] - rtl[i]) for i in range(m)]
    mx, mean = max(errs), sum(errs) / m
    worst = errs.index(mx)
    print("比对 %d 点: 最大误差 %d LSB (第 %d 行), 平均 %.3f LSB" % (m, mx, worst, mean))
    print("判据 ≤ ±2 LSB ->", "PASS" if mx <= 2 else "FAIL")


if __name__ == "__main__":
    coef = read_coef()
    print("载入 %s: %d 个系数" % (COEF_FILE, len(coef)))
    xs = gen_stimulus()
    g = golden(xs, coef)
    with open(GOLDEN_FILE, "w") as f:
        for v in g:
            f.write("%04X\n" % (v & 0xFFFF))
    print("已生成 %s (%d 样本) 与 %s (%d 行)" % (STIM_FILE, len(xs), GOLDEN_FILE, len(g)))
    if len(sys.argv) > 1 and sys.argv[1] == "cmp":
        compare(g)
