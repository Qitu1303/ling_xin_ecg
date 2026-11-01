"""gen_regmap.py —— 从 regs.py 生成对外接口定义（单一来源 → 三份产物）。

单一来源 = ecgb/regs.py，其余由本脚本生成、禁止手改：docs/regmap_onepager.md、
board/board_pkg/regmap.json、board/board_pkg/regs_map.vh。用法：python tools/gen_regmap.py。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = ROOT
BOARD = os.path.abspath(os.path.join(ROOT, '..', '..', 'board', 'board_pkg'))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ecgb import regs  # noqa: E402

REGISTERS = [
    dict(off=regs.REG_CTRL, name='CTRL', attr='RW', desc='控制寄存器',
         fields=[('[0]', 'en', '1=开采样（tick_gen 计数使能）'),
                 ('[1]', 'filt_en', '1=滤波链生效'),
                 ('[2]', 'bypass', '1=强制旁路原始样本（对照演示）'),
                 ('[3:2]', 'band', '带宽档位 00=40Hz(现状) 01=25Hz 10=15Hz 11=保留；RTL 未实现，写了不生效'),
                 ('[5..31]', 'reserved', '系数切换 / 复位等（预留未实现），RTL 实现前不要依赖')]),
    dict(off=regs.REG_CNT, name='SAMPLE_CNT', attr='RO', desc='上电以来 EOC 计数，只增不复位',
         fields=[('[31:0]', 'sample_cnt', '自检用：每秒增量 = 有效采样率')]),
    dict(off=regs.REG_FILT, name='FILT', attr='RO', desc='滤波输出',
         fields=[('[15:0]', 'filt', '滤波后码值'),
                 ('[30:16]', 'reserved', '保留'),
                 ('[31]', 'filt_valid', '20 ns 单周期脉冲，Python 轮询读不到，别当判据')]),
    dict(off=regs.REG_RAW, name='RAW', attr='RO', desc='原始样本 + 新数据标志',
         fields=[('[15:0]', 'sample_raw', 'XADC 原始码值'),
                 ('[30:16]', 'reserved', '保留'),
                 ('[31]', 'new_flag', '有新样本；读清：一次读操作即清零，同一轮询只读一次')]),
]

CTRL_COMBOS = [
    ('0x1', 'CTRL_RAW_PASS', '直通原始样本（M2 对照左半边）'),
    ('0x3', 'CTRL_FILTERED', '滤波输出（M2 对照右半边）'),
    ('0x5', 'CTRL_BYPASSED', '强制旁路'),
    ('0x0', '（无）', '停采样：计数冻结，XADC 不再被触发'),
    ('0x1 | band<<2', 'regs.ctrl_word(band=n)', '叠加带宽档位（[3:2]，RTL 未实现）'),
]

FENCE = chr(96) * 3
BT = chr(96)      # 反引号：Verilog 宏前缀，统一用 chr 写以免转义歧义
SQ = chr(39)      # 单引号：Verilog 字面量用
NL = chr(10)


def build_json():
    return dict(
        source='ecgb/regs.py（单一来源，勿手改本文件）',
        generated_by='tools/gen_regmap.py',
        ip_name=regs.IP_NAME_DEFAULT,
        base_address='0x%08X' % regs.IP_BASE_FALLBACK,
        range_bytes=regs.IP_RANGE_FALLBACK,
        bit_plan_authority='《三人数据流接口表.xlsx》Sheet④（2026-10-07 拍板：优先按表里的来）',
        notes=[
            '板上 FCLK_CLK0 = 31.25 MHz（实测）；PS 时钟由 boot 决定，Vivado 里写 50 MHz 不生效',
            '有效采样率 = sample_cnt 增量 / 实际耗时，是唯一权威；理论 31.25、2026-10-05 实测 19.53（Q-01）',
            'filt_valid 是单周期脉冲，Python 轮询读不到',
            'new_flag 读清，一次轮询只读一次 0x0C',
            '[3:2] 带宽档位与 [5..31] 复位/系数切换均为预留，RTL 未实现',
        ],
        registers=[dict(offset='0x%02X' % r['off'], name=r['name'], attr=r['attr'], desc=r['desc'],
                        fields=[dict(bits=b, name=n, desc=d) for b, n, d in r['fields']])
                   for r in REGISTERS],
        ctrl_combos=[dict(value=v, name=n, desc=d) for v, n, d in CTRL_COMBOS],
        conversion=dict(code_mid=regs.CODE_MID, code_full=regs.CODE_FULL,
                        divider_a0=regs.DIVIDER_A0,
                        volt='code / 65536 / 0.301',
                        note='A0 板载 2.32k+1k 分压，比值约 0.301；AD8232 输出直连 A0'),
    )


def md_text() -> str:
    L = []
    L.append('# 接口一页纸（由 tools/gen_regmap.py 生成）')
    L.append('')
    L.append('> **单一来源 = ecgb/regs.py**；本文件由脚本生成，手改会在下次生成时被覆盖。')
    L.append('> 位分配以《三人数据流接口表.xlsx》Sheet④ 为准（2026-10-07 拍板：优先按表里的来）。')
    L.append('> 生成命令：python tools/gen_regmap.py')
    L.append('')
    L.append('## 一、硬件侧：IP 寄存器（A 提供，B 只读）')
    L.append('')
    L.append('- IP 实例名：' + regs.IP_NAME_DEFAULT + '　基址：0x%08X' % regs.IP_BASE_FALLBACK +
             '　范围：' + str(regs.IP_RANGE_FALLBACK) + ' 字节')
    L.append('')
    L.append('| 偏移 | 名称 | 属性 | 位域 | 字段 | 说明 |')
    L.append('|---|---|---|---|---|---|')
    for r in REGISTERS:
        first = True
        for bits, name, desc in r['fields']:
            c1 = '0x%02X' % r['off'] if first else ''
            c2 = r['name'] if first else ''
            c3 = r['attr'] if first else ''
            L.append('| ' + c1 + ' | ' + c2 + ' | ' + c3 + ' | ' + bits + ' | ' + name + ' | ' + desc + ' |')
            first = False
    L.append('')
    L.append('### 控制字常用组合')
    L.append('')
    L.append('| 写值 | 宏 | 效果 |')
    L.append('|---|---|---|')
    for v, n, d in CTRL_COMBOS:
        L.append('| ' + v + ' | ' + n + ' | ' + d + ' |')
    L.append('')
    L.append('> 带宽档位用 regs.ctrl_word(band=n) 拼，别手写位运算：band 0/1/2/3 = 40/25/15/保留 Hz。')
    L.append('>')
    L.append('> 实现状态（2026-10-07）：`[3:2]` 与 `[5..31]` **A 侧 RTL 尚未实现**，写了不改变行为；')
    L.append('> B 侧 `PsAfe.set_band(n)` / `AcquisitionLoop(...)` 已按本表对齐，A 实现后 B 代码无需改。')
    L.append('>')
    L.append('> 已知位重叠：`[2]` 同时是 bypass 与档位低位，写档位 3 会连带把 bypass 拉高（表里待 A 处理）。')
    L.append('')
    L.append('### 码值 <-> 电压')
    L.append('')
    L.append('电压(V) = 码值 / ' + str(int(regs.CODE_FULL)) + ' / ' + str(regs.DIVIDER_A0) +
             '　（A0 板载分压约 0.301，AD8232 直连 A0）')
    L.append('')
    L.append('## 二、软件侧：B 对外 API（给 C 用）')
    L.append('')
    L.append(FENCE + 'python')
    L.append('from ecgb.ps_afe import PsAfe                 # 板级驱动（三级后端）')
    L.append('from ecgb.acq_loop import AcquisitionLoop      # 采集主循环')
    L.append('from ecgb.display_link import DisplayLink      # 与 C 的对接层')
    L.append('from ecgb.checkup import run_checkup           # 8 关自检')
    L.append('')
    L.append('afe  = PsAfe("design_1_wrapper.bit")')
    L.append('fs   = afe.measure_rate(3.0)                  # 实测采样率，唯一权威，禁止写死')
    L.append('link = DisplayLink(fs=fs)                     # 无 pynq 时自动退化，不抛异常')
    L.append('loop = AcquisitionLoop(afe, fs=fs, win_sec=8.0)')
    L.append('loop.run(seconds=120, on_snapshot=lambda s: link.push(s.wave, s.hr))')
    L.append('link.close(); afe.stop()')
    L.append(FENCE)
    L.append('')
    L.append('B 交给 C 的 Snapshot：')
    L.append('')
    L.append('| 字段 | 类型 | 语义 |')
    L.append('|---|---|---|')
    L.append('| wave | np.ndarray[uint16] | 最近 window_sec 秒样本，按时间正序，中心 32768 |')
    L.append('| fs | float | 实测有效采样率 (S/s) |')
    L.append('| hr | int 或 None | 心率；None 时画面显示 HR -- |')
    L.append('| hr_ok | bool | 质量是否可信 |')
    L.append('| dropped | int | 估算漏样数（硬件计数增量 - 软件收数） |')
    L.append('| n_total / n_hw | int | 软件累计 / 硬件计数 |')
    L.append('')
    L.append('C 侧调用约定（照 ecg_display.py 模块头）：disp.update(wave, fs, hr)；')
    L.append('wave 必须是 uint16、一维、长度 >=2（推荐 round(fs*8)）；fs 不能是 None / 0。')
    L.append('')
    L.append('## 三、改口径的规矩')
    L.append('')
    L.append('1. 只改 ecgb/regs.py（唯一来源）；')
    L.append('2. 跑 python tools/gen_regmap.py 重新生成三份产物；')
    L.append('3. 在群里说明改了什么、为什么 —— 不许只改代码不通知。')
    L.append('')
    return NL.join(L)


def vh_text() -> str:
    L = ['// regs_map.vh —— 由 tools/gen_regmap.py 生成，勿手改',
         '// 给 A 侧 Verilog 用：' + BT + 'include 本文件后直接用宏，避免地址口径漂移',
         BT + 'ifndef ECGB_REGS_MAP_VH',
         BT + 'define ECGB_REGS_MAP_VH',
         '']
    L.append(BT + 'define ECGB_CTRL          8' + SQ + 'h00   // 控制寄存器')
    L.append(BT + 'define ECGB_SAMPLE_CNT    8' + SQ + 'h04   // 上电以来 EOC 计数，只增不复位')
    L.append(BT + 'define ECGB_FILT          8' + SQ + 'h08   // 滤波输出')
    L.append(BT + 'define ECGB_RAW           8' + SQ + 'h0C   // 原始样本 + 新数据标志')
    L.append('')
    L.append(BT + 'define ECGB_CTRL_EN       1' + SQ + 'b1        // [0] 全局使能')
    L.append(BT + 'define ECGB_CTRL_FILT_EN  1' + SQ + 'b1 << 1   // [1] 滤波使能')
    L.append(BT + 'define ECGB_CTRL_BYPASS   1' + SQ + 'b1 << 2   // [2] 旁路')
    L.append(BT + 'define ECGB_BAND_LSB      2           // [3:2] 带宽档位（预留未实现）')
    L.append(BT + 'define ECGB_BAND_MASK    2' + SQ + 'b11 << ' + BT + 'ECGB_BAND_LSB')
    L.append('')
    L.append(BT + 'endif')
    L.append('')
    return NL.join(L)


def main() -> int:
    outs = []
    j = os.path.join(BOARD, 'regmap.json')
    with open(j, 'w', encoding='utf-8') as f:
        json.dump(build_json(), f, ensure_ascii=False, indent=2)
    outs.append(j)
    m = os.path.join(ROOT, 'docs', 'regmap_onepager.md')
    with open(m, 'w', encoding='utf-8') as f:
        f.write(md_text())
    outs.append(m)
    v = os.path.join(BOARD, 'regs_map.vh')
    with open(v, 'w', encoding='utf-8') as f:
        f.write(vh_text())
    outs.append(v)
    for p in outs:
        print('generated:', p)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())