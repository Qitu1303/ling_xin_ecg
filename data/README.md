# data — 测试数据与参考结果

| 文件 | 格式 | 说明 |
|------|------|------|
| `stimulus.hex` | 2048 行，每行 4 位十六进制 | XADC 行为模型的输入码值（uint16，中值 32768）。合成信号 = 0.8 Hz 有用波 + 0.15 Hz 基线漂移 + 50 Hz 工频干扰，采样率 500 S/s |
| `golden.hex` | 2048 行，每行 4 位十六进制 | Python 参考实现的滤波链输出，与 `stimulus.hex` 逐行对应 |
| `fir_coef.hex` | 63 行，每行 4 位十六进制 | 63 阶低通 FIR 的 Q1.15 系数：有符号和 = 32768（DC 增益 1.0）、对称、中心抽头 `0x1470` |

## 重新生成与比对

```bat
cd sim
python gen_stimulus.py        # 重新生成 ../data/stimulus.hex 与 ../data/golden.hex
run_sim.bat                   # RTL 仿真，输出 ../sim/results/output.hex
python gen_stimulus.py cmp    # 逐行比对 output.hex 与 golden.hex
```

三个文件同源：`fir_coef.hex` 同时被 `gen_stimulus.py`（Python 金标准）与 RTL 系数表使用；RTL 中的 63 个系数已直接烘焙进 `fir_lp_40hz.v`，运行时不再依赖 `.hex` 文件。

板上采集记录（`part01.csv` + `summary.json`）在 `board/records/`，字段与复核方法见 `board/README.md`。
