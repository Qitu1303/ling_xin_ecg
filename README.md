# 聆心 — 基于 PYNQ-Z2 的便携式实时心电监护系统

单导联心电采集与实时分析作品：AD8232 模拟前端 → XADC 等间隔采样 → PL 三级数字滤波 → PS 心率检测与插件化分析 → 网页 + HDMI 双路显示。面向家庭健康监护、教学实验与算法原型验证，不构成医疗诊断。

## 目录

| 目录 | 内容 | 开发期对应 |
|------|------|------------|
| `src/rtl/` | PL 侧 RTL 与独立集成顶层 `afe_top.v`（50 MHz 仿真/独立路径，不参与出 bit）；采样定时、XADC 封装、三级滤波（系数按 500 S/s 设计域） | `de_rtl` |
| `src/core/` | PS 侧 Python：采集主循环、心率检测、插件链、网页服务、板端驱动与工具 | `de_core` |
| `src/hdmi/` | HDMI 波形/心率渲染器 | `de_hdmi` |
| `sim/` | RTL testbench、XADC 行为模型、激励生成与离线回归 | — |
| `build/` | 可复现构建脚本、BD、XDC、自建 IP（`ip_repo/afe_ctrl_1_0/` 才是编进交付 bit 的那份 PL 逻辑）、综合/实现报告 | `vivado_rtl_prj` |
| `board/` | 上板包：v6 bit/hwh、可直接上传的 `board_pkg/`、6 组实测记录 | `de_core/board_pkg` |
| `data/` | 仿真激励、滤波器系数、Python 金标准 | — |
| `skill/` | 从开发过程提炼的 FPGA/PYNQ 技能包（v2.0） | `de_skill` |
| `report/` | 设计报告与大模型协作记录（按提交要求维护） | — |

### 两份 PL 源码的关系

仓库里有两条 PL 路径，用途不同，改之前先确认自己在改哪一份。

- **`build/ip_repo/afe_ctrl_1_0/` —— 编进交付 bit 的那一份。** `hdl/afe_ctrl_slave_lite_v1_0_AFE_AXI.v:331` 的 `tick_gen` 取 `CLK_FREQ = 31_250_000`、`RATE = 500`，与板上 `FCLK_CLK0` 实测 31.25 MHz 相配，产生 500 Hz `CONVST`。`component.xml` 的文件清单不含 `afe_top.v` 与 `xadc_wrap.v`，这两个文件不参与综合。
- **`src/rtl/` + `sim/tb_afe_top.v` —— 独立集成顶层与仿真路径。** `afe_top.v:26` 的 `CLK_FREQ = 50_000_000` 与 testbench 的 50 MHz 时钟（`tb_afe_top.v:45` 的 `#10`）成对，配合行为级 XADC 模型（`sim/xadc_wiz_0_sim.v:8` `FS = 500.0`）得到 500 S/s 的数据率，`data/golden.hex` 的逐行比对依赖这一组合。单边改任何一侧的时钟取值都会让金标准比对失效。

两处同名的 `filter_chain.v`、`hp_05hz.v`、`notch_50hz.v`、`fir_lp_40hz.v` 只有头部与行尾注释差异，`ALPHA_Q15`、`DLY`、63 个系数与端口完全相同，属同一设计的两份副本。若只改 `src/rtl/` 再重建位流，改动不会进入 bit。

## 关键指标

- 有效采样率：真机标定约 **31.3 S/s**。口径链条为 `CONVST` 500 Hz（`tick_gen` 的 `RATE=500`）→ XADC 通道平均 16（`build/bd/design_1.bd` 的 `CHANNEL_AVERAGING = 16`）→ **EOC 31.25 S/s**；三级滤波链由 EOC 驱动，故板上进样率即 31.25 S/s
- 滤波链：三级级联，仅占 **1 个 DSP48E1**（63 阶 FIR 串行 MAC + 系数烘焙）。三级系数全部按 fs = 500 S/s 设计域计算（0.5 Hz 高通 / 50 Hz 陷波 / 40 Hz 低通），而板上按 31.25 S/s 进样，全链无重采样环节 —— 板上的实际频率响应不等于这组指标，见 `report/tech-report.pdf` 第三部分「已知局限」
- 心率：演示口径 HR 72；间期 183 / 83 / 413 / 454 ms，18 组回归
- 显示：网页 60 fps 独立渲染；HDMI 1280×720@60 Hz；离屏清屏 127 ms → **2.6 ms**（约 49 倍）

## 复现步骤

### 1. 重建位流（可选）

环境：Vivado 2025.2，器件 `xc7z020clg400-1`。

```bat
cd build
vivado -mode batch -source build.tcl -tclargs work
```

产物：`build/work/design_1_wrapper.bit`、`design_1_wrapper.hwh`、`build/work/reports/*.rpt`。
构建脚本从 `build/bd/design_1.bd` 重建 Block Design，自建 IP 从 `build/ip_repo/` 引入，约束在 `build/constraints/`。仓库内 `build/reports/` 与 `board/` 下的 bit/hwh 是 2026-10-12 的 v6 实测版本（时序全通过），可直接上板，无需先重建。

### 2. 上板部署

1. 将 `board/design_1_wrapper.bit` 与 `board/design_1_wrapper.hwh` 上传到板卡同一目录（如 `/home/xilinx/jupyter_notebooks/`），**必须同名同目录**。
2. 将 `board/board_pkg/` 整包上传到 `/home/xilinx/jupyter_notebooks/ecgb_pkg/`。整包已含 `ecg_display.py`（HDMI 推屏需要它，它与 `board_demo.py` 同目录才可 import 成功），逐文件上传会漏掉推屏。
3. 硬件：AD8232 的 VCC 接 3.3 V、GND 接 GND、OUT 接 A0；HDMI 接显示器；网线接与 PC 同一网段。

### 3. 自检与运行

在 Jupyter 中：

```python
import sys
sys.path.insert(0, '/home/xilinx/jupyter_notebooks/ecgb_pkg')
from ecgb.checkup import run_checkup
rep = run_checkup('/home/xilinx/jupyter_notebooks/design_1_wrapper.bit',
                  out_dir='/home/xilinx/jupyter_notebooks/ecg_out', seconds=10)
print(rep.ok)      # 八关全 PASS 时为 True
```

或运行演示主循环：

```python
%cd /home/xilinx/jupyter_notebooks/ecgb_pkg
!python3 board_demo.py /home/xilinx/jupyter_notebooks/design_1_wrapper.bit . 120
```

浏览器访问 `http://<板卡IP>:8765` 打开网页监护界面；HDMI 输出 1280×720 波形与心率。八关依次为：写读、采样率标定、冻结、样本在动、输出量程、旁路对照、波形存盘、心率。

> **失败判读**：自检第 1 关回读 `0x0`、或输出里出现仿真横幅（`[SimAfe]`），含义是 bit 没找到，不是位流或板子坏了 —— `ecgb/ps_afe.py:54-61` 在打不开 bit 时会静默降级到仿真后端。第 1 参一律写绝对路径。

### 4. 离线验证（无板）

```bat
cd src\core
python tests\test_plugins_offline.py
```

覆盖 18 组用例（插件隔离、质量评估、报警、HRV、心律失常、间期、快速测量、网页服务、记录分片、板端口径长会话等），全部打印 `ok` 即通过。

### 5. RTL 仿真

```bat
cd sim
run_sim.bat
python gen_stimulus.py cmp
```

Verilog testbench 以 `+define+SIM_XADC_BEHAVIORAL` 启用 XADC 行为模型，读 `data/stimulus.hex`，输出写入 `sim/results/output.hex`；`gen_stimulus.py cmp` 与 `data/golden.hex` 逐行比对（本次 2047 点，最大误差 0 LSB）。详见 `sim/README.md`。

## 验收判据

| 项 | 判据 |
|----|------|
| 采样率 | `SAMPLE_CNT` 每秒增量约 31.3（15~32 为有效）。该计数是 **EOC 口径**（500 Hz `CONVST` 经 16 倍通道平均），不是 `CONVST` 口径 |
| 心率 | 接入模拟器后与源一致；31.25 S/s 演示口径 HR 72 |
| 网页 | 60 fps 刷新，SSE 推送 ≤10 Hz |
| HDMI | 1280×720@60 Hz，独立显示波形与心率 |
| 记录 | 600 s/段，样本号连续、无重复写 |

## 安全与边界

- 交付演示与验收只用信号源或心电模拟器取 BPM 真值，**不得将电极接入人体做测量**；开发调试期操作者本人的短时佩戴仅用于打通链路与核对波形形态，不构成任何测量或结论。
- 输出为节律类别与间期测量参考，不构成医疗诊断。
- 板上 Python 3.10 无 scipy / matplotlib；`board_pkg` 的采集与心率主链只依赖 numpy 与标准库。**HDMI 推屏是例外**：`ecg_display.py` 另需 PIL（`src/hdmi/ecg_display.py:23-27` 的 try-import，缺失即 raise）与字体文件（硬编码 `/usr/share/fonts/truetype/dejavu/`）。缺 PIL 时 `board_demo.py:119-124` 打印「本次不推屏」并退化为 `NullDisplay`，采集、心率与网页不受影响。板载是否已装 Pillow 需上板确认。

## 许可

源码与文档按 MIT 协议发布，见 `LICENSE`。`build/ip_repo/` 下含再分发的第三方 IP，其归属与许可见 `THIRD_PARTY_NOTICES.md`。

## 相关文档

- 构建与报告：`build/README.md`
- 上板与实测：`board/README.md`
- 仿真与离线回归：`sim/README.md`
- 数据格式：`data/README.md`
- 技能包：`skill/README.md`（Agent 入口 `skill/SKILL.md`）
- 设计报告与大模型协作记录：`report/`
