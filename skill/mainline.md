# 全流程主线：从想法到 PL+PS 项目上板

- **类型**：导航主文档（流程骨架）
- **适用场景**：新题目从零立项；或已有工程"现在卡在哪一阶段"的定位；团队协作时的分工接口。
- **使用方法**：按 S0→S9 顺序推进。每阶段先读「出口判据」，不满足不进下一阶段；每阶段「入口」列出自包含子件（零依赖可用）与 AMD 官方技能（已安装则优先调用），细节按需加载。
- **失效条件**：① 纯算法单点任务（只做 HLS 仿真验证）可只走 S2~S3；② 无 PS 的纯 FPGA 板卡可跳过所有 PS 相关条目；③ Versal/AIE 平台不适用（`vai-*`/`segcfg-*` 19 个官方技能不在本包覆盖范围）。
- **已验证效果**：本流程骨架来自一条真实项目链路（传感器采集 → PL 预处理 → PS 检测/显示 → 上板联调），按此走通"工具链搭建 → 自定义 AXI IP → BD 集成 → 6 个 bit 版本迭代 → 12 次上板验证"的完整闭环；S1 的预算表与 S8 的判据均有实测数据支撑。
- **来源**：本项目实战沉淀 + AMD 官方技能的方法论（`hls-architect` 的阶段门控、`vivado-*` 系列的构建/验证纪律、`hls-*` 的设计规则），映射关系见每阶段「官方技能」行。

---

## 阶段总览

| 阶段 | 一句话 | 主入口（自包含） | 官方技能（可选调用） | 出口判据 |
|---|---|---|---|---|
| S0 立项 | 板卡/工具链/镜像基线 | `pitfalls/pynq_onboard.md` K1-15 | — | 板卡能启动、版本三件套记录在案 |
| S1 系统规划 | PS/PL 切分与带宽预算 | `references/system_planning_pspl_split_and_bandwidth.md` | —（官方空白） | 切分表 + 带宽/资源预算表填满 |
| S2 设计 | HLS 或手写 RTL | `references/hls_design_discipline.md` | `hls-synthesizable`、`hls-pragma-scope`、`hls-dataflow`、`hls-pipeline`、`hls-burst-inference` | 可综合性/结构判据全过；或 lint 0 违规 |
| S3 仿真验证 | 自检 TB + 判定 | `references/simulation_verification_contract.md` | `vivado-simulate-rtl`、`hls-run-flow` | 显式 PASS 标记 + 有限运行 + 金标准比对 |
| S4 IP 配置与封装 | 单 IP 参数化、自定义 IP 打包 | `references/ip_config_and_timing_closure.md` §1、`templates/minimal_onboard_demo_project.md` | `vivado-ip-configurator` | 回读校验通过、component.xml 端口齐全 |
| S5 BD 集成 | PS7 + 互连 + 地址 + 时钟复位 | `templates/minimal_onboard_demo_project.md`、`pitfalls/vivado_bd_and_ip.md` | `vivado-ip-configurator`（单 IP） | `validate_bd_design` 无新增非白名单 CRITICAL |
| S6 综合/实现/时序 | 日志处置 + 时序收敛 | `references/rtl_static_verification_and_synth_logs.md`、`scripts/check_timing.py`、`pitfalls/clock_and_constraints.md` | `vivado-rtl-lint`、`vivado-rtl-elaboration-analysis`、`vivado-timing-methodology-checks` | `write_bitstream Complete!`；WNS/WHS 双判通过 |
| S7 bit 与交付 | 出包、定版、留证 | `prompts/03-vivado-tcl-bit-build.md`、`scripts/check_bit_pair.py`、`templates/deployment_version_checklist.md` | `vivado-revision-control` | 哈希台账齐全、bit+hwh 同名同目录 |
| S8 板上调试 | 部署、判活、排障 | `prompts/01`、`prompts/02`、`pitfalls/pynq_onboard.md`、`pitfalls/hdmi_and_display.md`、`templates/jupyter_staged_delivery.md` | `hw-ila-debug`、`hw-vio-debug`（有 JTAG 时） | 四指标达标 + 与离线基线一致 |
| S9 优化与收尾 | 设计期迭代 + 运行期归因 + 交接 | `references/hls_perf_loop.md`、`prompts/02`、`templates/onboard_run_report.md` | `hls-optimize`、`hls-ii-diagnose`、`hls-perf-pragma` | 优化有记账、成败留档；报告可被他人复核 |

---

## S0 立项：固定板卡与工具链基线 `[通用]`

**目标**：把"哪块板、哪个器件、哪版工具、哪版镜像"钉死，后续所有判据都以它为准。
**入口**：K1-15 上电启动三段判据；`references/系统规划` 的板卡选型清单。
**做法**：记录四元组（板卡型号 / 器件号 / Vivado 版本 / PYNQ 镜像版本）与板卡启动判据（电源灯→DONE→系统 LED→串口身份串）；新卡首次启动先重做卡、再查硬件。
**出口判据**：板上能进 Jupyter；工具链版本四元组写入版本核对表。
**常见走偏**：跳过镜像版本锁定直接查硬件；按"日志最后一行"反推启动卡点。

## S1 系统规划：PS/PL 切分与带宽预算 `[通用]`

**目标**：先决定"什么放 PL、什么放 PS、数据怎么过 AXI"，再动手写代码——这是官方技能也缺的正向流程。
**入口**：`references/system_planning_pspl_split_and_bandwidth.md`（切分决策表 / AXI 端口带宽预算 / Zynq-7000 资源频率预估 / 时钟复位域规划）。
**出口判据**：三张表填满——① 功能→执行域→理由；② 每条数据流的速率×位宽→所需带宽 vs 端口上限；③ 资源/频率/延迟预算与余量（≥30%）。
**常见走偏**：先写 RTL 再想数据怎么进 PS；用"峰值带宽"当预算而不留仲裁余量；把硬核（XADC/MIG/PCIe）放进自定义 IP。

## S2 设计：HLS 或手写 RTL `[HLS 通用 / RTL 通用]`

**目标**：产出"结构正确、可综合、可验证"的源文件。
**入口**：
- HLS 路线 → `references/hls_design_discipline.md`（可综合性 10 条 / pragma 放置 4 类 / dataflow 判据 / pipeline 规则 / burst 前置条件）；官方 `hls-architect`（架构规划：activity timeline + 三层结构）、`hls-synthesizable`、`hls-pragma-scope`、`hls-dataflow`、`hls-pipeline`、`hls-burst-inference`。
- 手写 RTL 路线 → S2 内先跑 lint（见 S6 入口），按 `templates/minimal_onboard_demo_project.md` 的 AXI 从机三处必改。
**出口判据**：HLS：可综合性 10 条全过、pragma 位置合法、dataflow canonical（若用）；RTL：lint 0 CRITICAL、0 非白名单 WARNING、elaborate 通过。
**常见走偏**：架构阶段提前写 `PIPELINE/UNROLL/PARTITION`（宏架构未定就微优化）；pragma 写在 `{` 之前（完全不生效）；`DATAFLOW` 与 `PIPELINE` 同 scope（互斥）。

## S3 仿真验证：用契约证明"对"，而不是"能跑" `[通用]`

**目标**：把"功能正确"变成可复核的证据。
**入口**：`references/simulation_verification_contract.md`（9 项输入契约 / 有限运行与 watchdog / TEST_PASS 终态标记 / 判定五门 / 诊断分级 / 最小复现 / 版本 bisect）；官方 `vivado-simulate-rtl`（XSim/UVM/覆盖率全套）、`hls-run-flow`（csim→csynth→cosim 次序门）。
**出口判据**：编译+elaborate 成功 ∧ 仿真正常返回 ∧ **显式 PASS 标记** ∧ 覆盖/断言门达标 ∧ 金标准逐点比对（有参考数据时）。
**常见走偏**：把"编译干净"或"仿真器正常退出"当功能证明；无界 `run all`；失败就改 RTL（先定级：design / testbench / environment / timeout / tool_defect）。

## S4 IP 配置与封装：参数一次设对、回读校验 `[Vivado 通用]`

**目标**：把 IP 参数化（含 PS7）与自定义 IP 打包做对。
**入口**：`references/ip_config_and_timing_closure.md` §1（需求台账 / 单 `-dict` / 回读校验三件套 / 覆盖率披露 / Tier2 恢复）；`templates/minimal_onboard_demo_project.md` Step 2~3（AXI 从机三处必改 + Re-Package）；官方 `vivado-ip-configurator`（137 个 Tcl 过程，已装直接用）。
**出口判据**：每个需求项有 `applied/default/unapplied:<原因>` 归宿；回读值与意图一致；`find_disabled` 为空。
**常见走偏**：逐参数 `set_property`（不可原子回滚）；先 dump 全参数空间；在 BD 模式下用未知 CONFIG 键（只发非致命警告、`catch` 返回 0，必须回读）；同名相似参数按名字选而不按文档描述选。

## S5 BD 集成：PS7 + 互连 + 地址 + 时钟复位 `[Zynq 通用]`

**目标**：把 PS 与 PL 连成可验证的系统。
**入口**：`templates/minimal_onboard_demo_project.md` Step 4~5（PS7 配置、SmartConnect、地址分配、Make External 实际端口名）；`pitfalls/vivado_bd_and_ip.md`（门控参数 `BD 41-721`、`axi_intc` 反直觉、孤儿网清理、硬核 BD 化）。
**出口判据**：`validate_bd_design` 零非白名单 CRITICAL；Address Editor 无重叠；时钟/复位域每域有源；hwh 中基址/实例/中断声明三组齐全。
**常见走偏**：`report_property` 当判据（内存值会撒谎，save 后 grep 磁盘 `.bd` 才算）；失败的 `connect_bd_net` 留孤儿网；按对话框端口列表判断配置（禁用端口也列出）。

## S6 综合/实现/时序：日志先处置、时序双判 `[通用]`

**目标**：从源文件到 bitstream，过程中所有报错都有归宿、时序有判据。
**入口**：`references/rtl_static_verification_and_synth_logs.md`（lint 闸门 / `[Synth 8-XXX]` 逐条处置 / 报告先出源码后改）；`scripts/check_timing.py`（WNS/WHS/结论句三判）；`pitfalls/clock_and_constraints.md`（VCCO/PDRC/BUFR→pblock 收敛）；官方 `vivado-rtl-lint`、`vivado-rtl-elaboration-analysis`、`vivado-timing-methodology-checks`（57 ID 闭环）。
**出口判据**：`write_bitstream Complete! 100%`；`check_timing.py` 判 PASS（WNS>0 且 WHS>0 且结论句 met）；方法学检查无未处理 CRITICAL。
**常见走偏**：综合绿就以为能出 bit（VCCO 在 place 爆、PLL/VCO 在 bitgen 爆）；只判 WNS 漏 hold；hold 违例不分类就下结论（全在同一结构=结构性偏斜可评估；散布多域=真问题）。

## S7 bit 与交付：哈希定版、目录只增不覆盖 `[通用]`

**目标**：产出可复现、可追溯、可回滚的交付物。
**入口**：`prompts/03-vivado-tcl-bit-build.md`（TCL 落盘四阶段）；`scripts/check_bit_pair.py`（配对+台账）；`templates/deployment_version_checklist.md`（四列核对）；官方 `vivado-revision-control`（5 步可重建工程 + `build.tcl`）。
**出口判据**：bit/hwh 同名同目录；台账 MD5 一致；版本目录含 README（改动/理由/时序前后/回滚路径）；`version_audit.py` 全 `[OK]`。
**常见走偏**：用目录名/文件名当版本；只比字节数（相邻版本 bit 可逐字节同长）；覆盖旧版本目录。

## S8 板上调试：先证版本、再读真值、最后重放 `[PYNQ 通用]`

**目标**：让板上行为可解释、可回归。
**入口**：`prompts/01-offline-green-board-deaf-debug.md`（三板斧）；`prompts/02-onboard-load-attribution.md`（负载类）；`pitfalls/pynq_onboard.md`、`pitfalls/hdmi_and_display.md`；`templates/jupyter_staged_delivery.md`（五阶段分格）；官方 `hw-ila-debug`/`hw-vio-debug`（有 JTAG 链路时）。
**出口判据**：四指标达标（poll/中途快进/recal/事件流构成）；与离线基线按"事件码多重集+卡片簇"一致；产物 `tee` 落盘。
**常见走偏**：先改代码再定位；用板载时钟判断"最新文件"；正式流程里插带收尾的独立格；把"看起来流畅"当结论。

## S9 优化与收尾：设计期迭代 + 运行期归因 + 交接 `[通用]`

**目标**：把性能问题变成"有假设、有度量、有记账"的迭代，并留下可交接的证据。
**入口**：`references/hls_perf_loop.md`（8 步循环 / 成本模型 / 停止准则 / II 诊断 / target_ti 换算）；`prompts/02`（运行期负载归因）；`templates/onboard_run_report.md`（复盘固化）；官方 `hls-optimize`、`hls-ii-diagnose`、`hls-perf-pragma`。
**出口判据**：每个优化尝试有"假设→预期→实测→结论"记录（失败也提交留档）；停止条件明确（连续 3 次无改善 / 受限资源 >60% / 边际 <5%）；交付报告含判别实验与状态标记。
**常见走偏**：一次改多个变量；只看汇总不看"pragma 被忽略"警告；把 cosim/impl 当内循环（昂贵步骤是门，不是橡皮章）。

---

## 平台适配与官方技能调用约定

- **已安装官方技能时**：S2/S3/S4/S6/S9 的官方技能是"完整流程执行器"，本包对应 references 是其**规则精要**（用于未安装环境与快速自查）。两者结论冲突时，以工具真实输出与官方参考文档为准。
- **未安装官方技能时**：本包 references 自包含可用，规则出处均标注官方技能名与文档（UG901/UG902/UG906/UG1399/UG949）。
- **平台不适用的官方技能**：`vai-*`（AI Engine/VAIML，12 个）与 `segcfg-*`（Versal 分段配置，7 个）不适用于 Zynq-7000；仅 `vai-perf-analysis` 的"compute vs memory bound"归因思路可借鉴到 PL 带宽分析。
- **本包自有的**：板上调试（S7/S8）与部署版本一致性（S7）无官方等价物，是全流程的独有环节。
