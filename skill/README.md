# 通用 FPGA/PYNQ「想法 → PL+PS 项目 → 上板」技能包

> 定位：把「一支队伍的实战沉淀」与「AMD 官方 HLS/Vivado 技能族的规则判据」合并为**可直接复用**的技能包。目标读者是拿到陌生题目的下一位工程师 / Agent。
> 评价口径：换题目、换板卡（PYNQ/Zynq 系列）后，每一项能否直接套用自己的工程。
> 主入口是 **[mainline.md](mainline.md)**；Agent 加载入口见同目录 `SKILL.md`。

---

## 一、包体索引（22 项）

### 全流程主线（1）

| # | 子件 | 适用场景（一句话） | 失效条件（摘要） |
|---|---|---|---|
| M0 | [mainline.md](mainline.md) | 从想法到上板 10 阶段导航：每阶段给入口子件、官方技能、出口判据、常见走偏 | 纯单点任务可跳读；Versal/AIE 平台不适用 |

### 系统规划（新造，1）

| # | 子件 | 适用场景 | 失效条件 |
|---|---|---|---|
| R1 | [references/system_planning_pspl_split_and_bandwidth.md](references/system_planning_pspl_split_and_bandwidth.md) | 立项决策：PS/PL 切分表、AXI 带宽/HP 口预算、资源频率预估、时钟复位域规划 | 器件参数与型号绑定，换器件须按手册重填；折减系数需上板标定 |

### 官方规则蒸馏件（5）——取规则弃工具，自包含

| # | 子件 | 蒸馏来源 | 失效条件（摘要） |
|---|---|---|---|
| R2 | [references/hls_design_discipline.md](references/hls_design_discipline.md) | `hls-synthesizable`/`pragma-scope`/`dataflow`/`pipeline`/`burst-inference`/`flattenable`/`array-to-stream` | 纯 RTL 不适用；旧版本个别规则不同，以本机日志为准 |
| R3 | [references/hls_perf_loop.md](references/hls_perf_loop.md) | `hls-architect`/`hls-optimize`/`hls-ii-diagnose`/`hls-perf-pragma` | 假设"单 DATAFLOW + 三层结构"，异形工程只取判据；成本模型按中小 kernel |
| R4 | [references/rtl_static_verification_and_synth_logs.md](references/rtl_static_verification_and_synth_logs.md) | `vivado-rtl-lint`/`vivado-rtl-elaboration-analysis` | 查不出功能错误；8-XXX 编号随版本变化，原则稳定 |
| R5 | [references/simulation_verification_contract.md](references/simulation_verification_contract.md) | `vivado-simulate-rtl` | 不绑定具体仿真器；后综合模式需对应阶段已完成 |
| R6 | [references/ip_config_and_timing_closure.md](references/ip_config_and_timing_closure.md) | `vivado-ip-configurator`/`timing-methodology-checks`/`revision-control` | 不含 137 个 `ipcfg::*` 过程本体；TIMING ID 以本机输出为准 |

### 提示词工作流（3）

| # | 子件 | 适用场景 | 失效条件（摘要） |
|---|---|---|---|
| P1 | [prompts/01-offline-green-board-deaf-debug.md](prompts/01-offline-green-board-deaf-debug.md) | 离线全绿、上板无现象：MD5 定版本 → 读板上真值 → 拉回记录重放 | 板上日志未 `tee` 落盘则退化为猜测 |
| P2 | [prompts/02-onboard-load-attribution.md](prompts/02-onboard-load-attribution.md) | 丢样/卡顿/误报：定基准 → 减法归因 → 单杠杆矩阵 → 四指标判读 | 基准参数未定版无法横向比较；多杠杆同改归因失效 |
| P3 | [prompts/03-vivado-tcl-bit-build.md](prompts/03-vivado-tcl-bit-build.md) | 不开 GUI 程序化 BD 改造并出 bit：TCL 落盘 → 分批 validate → 磁盘 .bd 核对 → hwh grep → MD5 定版 | 依赖 TCL/batch；纯 RTL 工程只适用阶段 2~3 |

### 案例模板（4）

| # | 子件 | 适用场景 | 失效条件（摘要） |
|---|---|---|---|
| T1 | [templates/minimal_onboard_demo_project.md](templates/minimal_onboard_demo_project.md) | 自定义 AXI IP → BD → bit → PYNQ Python 最小链路 + 10 条坑表 | 非 AXI-Lite / 非 Zynq 需替换对应步骤 |
| T2 | [templates/onboard_run_report.md](templates/onboard_run_report.md) | 板上故障复盘与交接：五段结构 + 弱判据反例库 | 只写结论不写判别实验的报告无法复核 |
| T3 | [templates/deployment_version_checklist.md](templates/deployment_version_checklist.md) | 源树↔部署包↔板上三层版本一致性登记 | 第三列一致推不出第四列（板上落点/缓存） |
| T4 | [templates/jupyter_staged_delivery.md](templates/jupyter_staged_delivery.md) | PC 预演 → 上传核对 → 板上分格判活 → 逐组件二分 | 依赖 Jupyter 交付形态；GBK 控制台需设 UTF-8 |

### 校验脚本（4）——零依赖、双端可跑

| # | 脚本 | 校验什么 | 判读依据 |
|---|---|---|---|
| S1 | [scripts/check_bit_pair.py](scripts/check_bit_pair.py) | bit/.hwh 配对 + 哈希台账 + hwh 结构断言 | 哈希为主、字节为辅（相邻版本 bit 可同长） |
| S2 | [scripts/version_audit.py](scripts/version_audit.py) | 源树 vs 部署包逐文件哈希审计（只读） | 缺失/不同/多余三类差异 + 退出码 |
| S3 | [scripts/replay_records.py](scripts/replay_records.py) | 板上记录完整性/连续性/时基口径/基线比对 | 分片起点可中途；时基是合成口径非墙钟 |
| S4 | [scripts/check_timing.py](scripts/check_timing.py) | timing summary：WNS/TNS/WHS/THS + 结论句双判 + 按域细分 | 只判 WNS 会漏 hold 违例 |

### 踩坑清单（4 篇，53 条）

| # | 子件 | 覆盖范围 | 条目数 |
|---|---|---|---|
| K1 | [pitfalls/pynq_onboard.md](pitfalls/pynq_onboard.md) | Overlay 语义、Jupyter 落点/缓存、板载时钟、板上 numpy、取值方式 | 15 |
| K2 | [pitfalls/vivado_bd_and_ip.md](pitfalls/vivado_bd_and_ip.md) | PS7 门控参数、axi_intc、孤儿网、packager、vlog 局限、hwh 落点 | 16 |
| K3 | [pitfalls/clock_and_constraints.md](pitfalls/clock_and_constraints.md) | bitstream 不应用 PS7 时钟、平均分频、VCCO/PDRC、BUFR→pblock、32bit 溢出 | 8 |
| K4 | [pitfalls/hdmi_and_display.md](pitfalls/hdmi_and_display.md) | 零自写 RTL、720p、出图顺序、判活、采集卡伪坑、显示性能三板斧 | 14 |

---

## 二、上手路线图（10 阶段）

```
S0 立项 ────────── K1-15 启动判据 + 版本四元组（板卡/器件/Vivado/镜像）
S1 系统规划 ────── R1 四张表：切分 / 带宽 / 资源频率 / 时钟复位
S2 设计 ────────── HLS：R2 五节规则；RTL：R4 §1 lint 闸门
S3 仿真 ────────── R5 契约：9 项输入 → 有限运行 → 显式 PASS → 金标准
S4 IP 配置 ────── R6 §1 台账/单-dict/回读三件套 + T1 Step2~3
S5 BD 集成 ────── T1 Step4~5 + K2 排障（41-721 / axi_intc / 孤儿网）
S6 综合/时序 ──── R4 §2 日志处置 + S4 check_timing + K3（DRC/pblock）
S7 bit 交付 ───── P3 TCL 四阶段 + S1 check_bit_pair + T3 四列核对
S8 板上调试 ───── T4 分格 + P1 三板斧 / P2 负载归因 + K1/K4
S9 优化收尾 ───── R3 优化循环 + P2 运行期归因 + T2 复盘报告
```

**一句话记忆**：规划先填四张表 → 设计过规则闸门 → 仿真要显式 PASS → 构建哈希定版 → 板上四指标 + 基线对照。

## 三、通用性等级

- **[通用]**：与板卡/平台无关（哈希定版本、弱判据教训、验证纪律）。
- **[Vivado 通用]**：任何 Vivado IPI 工程适用（DRC 码、packager 行为、TCL 语义、lint 规则）。
- **[Zynq 通用]**：Zynq-7000 系适用（PS7/SLCR/XADC/HP 口；MPSoC 按 UG1085 类推）。
- **[PYNQ 通用]**：PYNQ 框架适用（Overlay/`pynq.lib.video`/Jupyter；2.x 与 3.x 差异已注明）。

板级定值（bank VCCO、球脚号、分压比）**数值不通用、方法通用**；所有示例路径已参数化（`<BOARD>`、`<BIT_PATH>`、`<PKG>`）。

## 四、每一项的五元组写法

| 字段 | 含义 |
|---|---|
| **适用场景** | 什么情况下用这一项 |
| **使用方法** | 步骤 / 模板正文 / 命令行 |
| **失效条件** | 什么情况下不成立、需要换路（写实） |
| **已验证效果** | 量化结论：项目实测数据，或官方技能的验证规则（注明依据 UG901/UG902/UG906/UG1399/UG949） |
| **来源** | 从哪次失败或观察总结得出（脱敏后表述）/ 蒸馏自哪个官方技能 |

## 五、经验来源、官方技能分工与脱敏声明

**来源一（实战）**：一支队伍约两个月的 FPGA 竞赛工程实做——PYNQ 板卡全流程（自定义 AXI IP、传感器采集链、HDMI 显示、板上组件与网页监视），含 6 个 bit 版本迭代、12 次板上联调记录、40+ 条踩坑档案。示例统一为"某传感器采集演示"中性案例。

**来源二（官方蒸馏）**：AMD 官方技能 47 个中，Zynq-7000 适用的 28 个被蒸馏为 5 份规则件（R2~R6），**取规则与判据、弃工具绑定**（Vitis IDE / Vivado MCP 调用改写为 batch TCL 与自包含规则）。

| 官方技能族 | 本包对应 | 分工 |
|---|---|---|
| `hls-architect`/`synthesizable`/`pragma-scope`/`dataflow`/`pipeline`/`burst-inference`/`optimize`/`ii-diagnose`/`perf-pragma` 等 22 个 | R2、R3 | 官方执行完整流程；本包给规则精要（未安装时可用） |
| `vivado-rtl-lint`/`rtl-elaboration-analysis`/`simulate-rtl`/`ip-configurator`/`timing-methodology-checks`/`revision-control` | R4、R5、R6 | 官方带逐 ID 指南与脚本；本包给流程纪律与高频判据 |
| `hw-ila-debug`/`hw-vio-debug` | 不蒸馏（S8 提示调用） | 需 JTAG 链路，PYNQ 常态开发无 JTAG |
| `vai-*`（12）/`segcfg-*`（7） | 不蒸馏 | Versal/AIE 平台不适用；仅"compute vs memory bound"归因思路可借鉴 |

**工具链参考版本**：Vivado 2025.2 + PYNQ 3.1.1（Zynq-7000）。以手头版本为准；跨版本差异在条目内注明（最典型：`.hwh` 落点移到 `<工程>.gen/sources_1/bd/<bd>/hw_handoff/`）。

## 六、复现与反馈

- 脚本自检：`python scripts/<名> --help` 每个参数有说明；四个脚本只用标准库，无第三方依赖。
- 若某条经验在你的题目/板卡上失效，按 T2 模板记录"现象/判别实验/根因"并回填对应条目的失效条件——这是本包的迭代方式。
