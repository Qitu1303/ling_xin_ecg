"""regs.py —— 寄存器口径（**唯一来源**：要改先改这里，再同步 A/C 与 tools/gen_regmap.py）。

板上 IP 实例名 afe_ctrl_0，基址 0x43C00000（.hwh 实测，PYNQ 侧优先用 ol.afe_ctrl_0）；
板载 FCLK_CLK0 = 31.25 MHz。
"""

# ---- 寄存器偏移（字节） ----
REG_CTRL = 0x00   # RW  控制
REG_CNT  = 0x04   # RO  [31:0] sample_cnt
REG_FILT = 0x08   # RO  [15:0] filt,  [31] filt_valid（**单周期脉冲，轮询读不到，别拿它做判据**）
REG_RAW  = 0x0C   # RO  [15:0] sample_raw(=xadc_do_i), [31] new_flag（**读清**：读一次就清）

# ---- 0x00 控制位（PS 写） ----
# 位分配以团队《三人数据流接口表.xlsx》Sheet④ 为准：
#   [0] en、[1] filt_en、[2] bypass、[3:2] 带宽档位（预留）、[5..31] 系数切换/复位等（预留）
CTRL_EN       = 1 << 0   # 全局使能；0 = 冻结采样（tick_gen 计数保持清零）
CTRL_FILT_EN  = 1 << 1   # 滤波使能；0 = 滤波链不走（配合 bypass 看原始样本）
CTRL_BYPASS   = 1 << 2   # 1 = 直通原始样本（对照演示用）；语义见 filter_chain.v

# [3:2] 带宽档位（BA-04）：位置预留，**当前 RTL 未实现，写了不生效**
BAND_LSB = 2
BAND_MASK = 0b11 << BAND_LSB
BAND = {
    0b00: "40Hz低通",    # 现状（filter_chain 默认）
    0b01: "25Hz低通",    # 预留
    0b10: "15Hz低通",    # 预留
    0b11: "保留",
}
# ⚠️ 档位依赖 notch 的 DLY = fs/50：fs 从 500 变到别的值时必须一并重算，否则工频陷波失效。

# 常用组合
CTRL_RAW_PASS = CTRL_EN                 # 0x1 直通原始样本（M2 对照的左半边）
CTRL_FILTERED = CTRL_EN | CTRL_FILT_EN  # 0x3 滤波输出（M2 对照的右半边）
CTRL_BYPASSED = CTRL_EN | CTRL_BYPASS   # 0x5 强制旁路


def ctrl_word(en: bool = True, filt_en: bool = False, bypass: bool = False,
              band: int | None = None) -> int:
    """按位定义拼一个 0x00 控制字，避免调用方各自手写魔法数。

    band=None 表示不动档位（写 0）；band 取 0..3，对应 BAND 字典。
    """
    w = 0
    if en:
        w |= CTRL_EN
    if filt_en:
        w |= CTRL_FILT_EN
    if bypass:
        w |= CTRL_BYPASS
    if band is not None:
        if not (0 <= int(band) <= 3):
            raise ValueError(f"带宽档位只能是 0..3，收到 {band}")
        w |= (int(band) << BAND_LSB) & BAND_MASK
    return w

IP_NAME_DEFAULT = "afe_ctrl_0"
IP_BASE_FALLBACK = 0x43C00000
IP_RANGE_FALLBACK = 0x10

# ---- XADC 码值域换算（A0 板载分压比 0.301，见 新模块划分.md 已确认决策 1） ----
CODE_MID = 32768.0        # ≈ 基线 1.65 V
CODE_FULL = 65536.0
DIVIDER_A0 = 0.301        # 板载 2.32k + 1k 分压比


def code_to_volt(code):
    """XADC 无符号码值 → A0 输入端真实电压（V）。"""
    return float(code) / CODE_FULL / DIVIDER_A0


def volt_to_code(volt):
    """A0 输入端电压（V）→ XADC 无符号码值（float，需自行取整）。"""
    return float(volt) * DIVIDER_A0 * CODE_FULL


# ---- 采样率口径：代码一律按"实测标定"处理，禁止写死 500 ----
# CONVST 由 tick_gen(RATE=500) 产生，但 Channel Averaging=16：预期 31.25 EOC/s，实测仅 19.53。
# 结论：ps_afe 的有效样本率 fs_eff = 实测 sample_cnt 增量（每秒），不假设理论值。
FS_THEORY = 31.25         # 理论值（只用于对照打印，不作为算法输入）
FS_MEASURED_1005 = 19.53  # 实测值（历史批次硬件）
