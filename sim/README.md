# sim — 仿真或验证脚本与结果

## 内容

| 文件 | 说明 |
|------|------|
| `tb_afe_top.v` | AFE 顶层 testbench：施加 2048 个样本，检查采样计数、滤波输出点数与 X 态 |
| `xadc_wiz_0_sim.v` | XADC Wizard 行为模型（`+define+SIM_XADC_BEHAVIORAL` 时启用），读 `../data/stimulus.hex` |
| `gen_stimulus.py` | 生成 `data/stimulus.hex` / `data/golden.hex`，并与 `results/output.hex` 逐行比对 |
| `run_sim.bat` | 一键编译 + 精化 + 运行（Vivado xsim） |
| `results/` | 本次运行的 `output.hex`、`compare.log`、`xsim.log`、`ps_offline_regression.log` |

## 运行

环境：Vivado 2025.2，`xvlog` / `xelab` / `xsim` 在 PATH 中（或在 Vivado Tcl Shell 里执行）。

```bat
cd sim
run_sim.bat
python gen_stimulus.py cmp
```

`run_sim.bat` 内部：`xvlog -sv -d SIM_XADC_BEHAVIORAL` → `xelab` → `xsim -runall`，输出写入 `results/output.hex`。批处理里调用 Vivado 的 `.bat` 必须加 `call`，否则控制权不会返回。

## 本次结果（2026-10-31）

```text
【SUCCESS】采样计数自检正确! sample_cnt = 2048
【SUCCESS】滤波输出 2047 点（输入 2048 点，差值 1 = 流水线延时）
比对 2047 点: 最大误差 0 LSB (第 0 行), 平均 0.000 LSB
判据 ≤ ±2 LSB -> PASS
```

- 三条不变量全部满足：有效采样数 = 2048；滤波输出 2047 点（FIR 串行 MAC 的 1 拍有效域延时）；输出无 X。
- 与 Python 金标准逐行比对：2047 点最大误差 **0 LSB**，平均 0.000 LSB。
- `results/ps_offline_regression.log` 是 PS 侧 18 组离线回归的完整输出，全部 `ok`。

## 离线回归（无板、无 pytest）

```bat
cd ..\src\core
python tests\test_plugins_offline.py
```

覆盖插件宿主隔离、质量评估、分级报警、HRV、心律失常、间期、30 s 快速测量、网页服务、记录分片、板端口径长会话等 18 组用例。全部打印 `ok`、末尾 `结果：全部通过` 即通过。

## 说明

- `stimulus.hex` 与 `golden.hex` 由 `gen_stimulus.py` 用同一份 `data/fir_coef.hex` 生成，避免双口径。
- 行为模型文件缺失时会退回内置解析式波形（0.8 Hz 信号 + 0.15 Hz 基线漂移 + 50 Hz 工频），此时不与 `golden.hex` 逐行对齐。
- 超时保护按 64 位时间计算（`2 ms × (NSAMP+100)`），避免 32 位溢出导致提前结束。

## 仿真的采样率域与板上的差别

仿真路径的数据率是 500 S/s：testbench 给 50 MHz 时钟（`tb_afe_top.v:45` 的 `#10`），`afe_top.v:26` 的 `tick_gen` 取 `CLK_FREQ = 50_000_000`、`RATE = 500`，行为级 XADC 模型按 `FS = 500.0` 出样（`xadc_wiz_0_sim.v:8`）。三级滤波的系数也正是按 fs = 500 S/s 计算的（`gen_fir_coef.py:11` `FS = 500.0`），所以本节展示的 0.5 Hz 高通 / 50 Hz 陷波 / 40 Hz 低通就是它们在 500 S/s 域的设计行为。

交付 bit 走的是另一条路径：板上 `FCLK_CLK0` 实测 31.25 MHz，`build/ip_repo/afe_ctrl_1_0/hdl/...AFE_AXI.v:331` 把 `CLK_FREQ` 取为 `31_250_000` 使 `CONVST` 仍为 500 Hz，再经 XADC `CHANNEL_AVERAGING = 16` 得到 EOC = 31.25 S/s；滤波链由该 EOC 驱动（同文件 `:341-346` 的 `.in_valid (xadc_eoc_i)`），全链没有重采样或抽取环节。

由此两条：

- 本节「2047 点最大误差 0 LSB」证明的是 RTL 与 Python 金标准在**同一 500 S/s 域内的定点算术一致**，不证明板上滤波达到 0.5~40 Hz 的设计指标。
- tb 的 50 MHz 与 `afe_top.v` 的 `CLK_FREQ` 必须成对修改，单边改动会让 `golden.hex` 比对失效。

板上频率响应与设计指标的偏差见 `report/tech-report.pdf` 第三部分「已知局限」。
