# 接口一页纸（由 tools/gen_regmap.py 生成）

> **单一来源 = ecgb/regs.py**；本文件由脚本生成，手改会在下次生成时被覆盖。
> 位分配以《三人数据流接口表.xlsx》Sheet④ 为准（2026-10-07 拍板：优先按表里的来）。
> 生成命令：python tools/gen_regmap.py

## 一、硬件侧：IP 寄存器（A 提供，B 只读）

- IP 实例名：afe_ctrl_0　基址：0x43C00000　范围：16 字节

| 偏移 | 名称 | 属性 | 位域 | 字段 | 说明 |
|---|---|---|---|---|---|
| 0x00 | CTRL | RW | [0] | en | 1=开采样（tick_gen 计数使能） |
|  |  |  | [1] | filt_en | 1=滤波链生效 |
|  |  |  | [2] | bypass | 1=强制旁路原始样本（对照演示） |
|  |  |  | [3:2] | band | 带宽档位 00=40Hz(现状) 01=25Hz 10=15Hz 11=保留；RTL 未实现，写了不生效 |
|  |  |  | [5..31] | reserved | 系数切换 / 复位等（预留未实现），RTL 实现前不要依赖 |
| 0x04 | SAMPLE_CNT | RO | [31:0] | sample_cnt | 自检用：每秒增量 = 有效采样率 |
| 0x08 | FILT | RO | [15:0] | filt | 滤波后码值 |
|  |  |  | [30:16] | reserved | 保留 |
|  |  |  | [31] | filt_valid | 20 ns 单周期脉冲，Python 轮询读不到，别当判据 |
| 0x0C | RAW | RO | [15:0] | sample_raw | XADC 原始码值 |
|  |  |  | [30:16] | reserved | 保留 |
|  |  |  | [31] | new_flag | 有新样本；读清：一次读操作即清零，同一轮询只读一次 |

### 控制字常用组合

| 写值 | 宏 | 效果 |
|---|---|---|
| 0x1 | CTRL_RAW_PASS | 直通原始样本（M2 对照左半边） |
| 0x3 | CTRL_FILTERED | 滤波输出（M2 对照右半边） |
| 0x5 | CTRL_BYPASSED | 强制旁路 |
| 0x0 | （无） | 停采样：计数冻结，XADC 不再被触发 |
| 0x1 | band<<2 | regs.ctrl_word(band=n) | 叠加带宽档位（[3:2]，RTL 未实现） |

> 带宽档位用 regs.ctrl_word(band=n) 拼，别手写位运算：band 0/1/2/3 = 40/25/15/保留 Hz。
>
> 实现状态（2026-10-07）：`[3:2]` 与 `[5..31]` **A 侧 RTL 尚未实现**，写了不改变行为；
> B 侧 `PsAfe.set_band(n)` / `AcquisitionLoop(...)` 已按本表对齐，A 实现后 B 代码无需改。
>
> 已知位重叠：`[2]` 同时是 bypass 与档位低位，写档位 3 会连带把 bypass 拉高（表里待 A 处理）。

### 码值 <-> 电压

电压(V) = 码值 / 65536 / 0.301　（A0 板载分压约 0.301，AD8232 直连 A0）

## 二、软件侧：B 对外 API（给 C 用）

```python
from ecgb.ps_afe import PsAfe                 # 板级驱动（三级后端）
from ecgb.acq_loop import AcquisitionLoop      # 采集主循环
from ecgb.display_link import DisplayLink      # 与 C 的对接层
from ecgb.checkup import run_checkup           # 8 关自检

afe  = PsAfe("design_1_wrapper.bit")
fs   = afe.measure_rate(3.0)                  # 实测采样率，唯一权威，禁止写死
link = DisplayLink(fs=fs)                     # 无 pynq 时自动退化，不抛异常
loop = AcquisitionLoop(afe, fs=fs, win_sec=8.0)
loop.run(seconds=120, on_snapshot=lambda s: link.push(s.wave, s.hr))
link.close(); afe.stop()
```

B 交给 C 的 Snapshot：

| 字段 | 类型 | 语义 |
|---|---|---|
| wave | np.ndarray[uint16] | 最近 window_sec 秒样本，按时间正序，中心 32768 |
| fs | float | 实测有效采样率 (S/s) |
| hr | int 或 None | 心率；None 时画面显示 HR -- |
| hr_ok | bool | 质量是否可信 |
| dropped | int | 估算漏样数（硬件计数增量 - 软件收数） |
| n_total / n_hw | int | 软件累计 / 硬件计数 |

C 侧调用约定（照 ecg_display.py 模块头）：disp.update(wave, fs, hr)；
wave 必须是 uint16、一维、长度 >=2（推荐 round(fs*8)）；fs 不能是 None / 0。

## 三、改口径的规矩

1. 只改 ecgb/regs.py（唯一来源）；
2. 跑 python tools/gen_regmap.py 重新生成三份产物；
3. 在群里说明改了什么、为什么 —— 不许只改代码不通知。
