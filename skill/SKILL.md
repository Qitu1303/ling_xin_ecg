---
name: de_skill
description: 通用 FPGA/PYNQ「想法 → PL+PS 完整项目 → 上板」全流程技能包。当任务涉及以下场景时应使用本技能：① 立项与系统规划（PS/PL 功能切分、AXI 带宽预算、资源频率预估、板卡选型）；② 设计与验证（HLS 可综合性/pragma 放置/dataflow/pipeline/burst 判据、RTL lint 与综合日志处置、自检 testbench 与仿真判定）；③ Vivado 工程构建（IP 参数化与回读校验、Block Design 集成、时序方法学闭环、TCL 程序化出 bit、可重建工程）；④ PYNQ 部署与板上调试（Overlay/bit+hwh 交付、Jupyter 上板流程、板上日志判读、负载归因）；⑤ 排查"离线全绿、上板无现象"类问题；⑥ 性能优化与版本一致性校验。含全流程主线、提示词工作流、案例模板、校验脚本、踩坑清单与官方规则蒸馏件，换题目、换板卡（PYNQ/Zynq 系列）仍可复用。
metadata:
  author: FPGA 竞赛队伍经验蒸馏 + AMD 官方技能规则蒸馏
  version: "2.0"
  references:
    - mainline.md
    - README.md
    - references/
    - prompts/
    - templates/
    - scripts/
    - pitfalls/
---

# Skill: FPGA/PYNQ 想法到上板全流程技能包

## Description / Purpose

本技能把两处经验合并为可直接复用的技能包：① 一支队伍在「PYNQ 板卡 + Vivado」全流程开发中反复踩坑、反复验证后的实战沉淀；② AMD 官方 HLS/Vivado 技能族的规则与判据（取规则、弃工具绑定）。

- **Expected outcome**：拿到本技能的 Agent / 工程师，能按 **10 阶段主线**（立项 → 系统规划 → 设计 → 仿真 → IP 配置 → BD 集成 → 综合时序 → bit 交付 → 板上调试 → 优化收尾）完成从想法到 PL+PS 项目上板的闭环，并在每阶段用明确判据验收。
- **Prerequisites**：了解 Verilog/C++ 与 Vivado 基本操作；目标板为 PYNQ 系列（Zynq-7000）或可类推的 Zynq MPSoC；Python 3.8+（脚本仅标准库）。HLS 路线需可综合的 C/C++ 源码。
- **硬条款**：所有"已验证效果"来自真实板测/构建记录或官方技能验证规则；脚本结论必须来自工具真实输出——禁止臆造判读结果。

## 包体位置与加载方式

子件全部在本文件所在目录下，主入口是 **[mainline.md](mainline.md)**（10 阶段导航），总索引见 **[README.md](README.md)**。

- 交付/评审视角（人工阅读）：`mainline.md` → `README.md` → 按阶段取子件。
- Agent 加载视角（CodeBuddy）：将本目录整体拷入 `~/.codebuddy/skills/de_`，用 `use_skill` 验证；SKILL.md 保持精简作 L1 导航，细节按需读子件（渐进披露：L2 = 本目录各子件，L3 = `references/` 规则细节）。

## When to Use

| 你的处境 | 入口 |
|---|---|
| 新题目从零开始 / 判断现在卡在哪一阶段 | `mainline.md` |
| 立项：PS/PL 怎么切、带宽/资源/频率够不够 | `references/system_planning_pspl_split_and_bandwidth.md` |
| 写 HLS：能不能综合、pragma 放哪、dataflow/pipeline/burst | `references/hls_design_discipline.md` |
| HLS 优化：II 不达标、资源异常、吞吐目标换算 | `references/hls_perf_loop.md` |
| 手写 RTL：改完先自检、综合报 8-XXX | `references/rtl_static_verification_and_synth_logs.md` |
| 仿真：TB 怎么判、日志怎么读、失败怎么分级 | `references/simulation_verification_contract.md` |
| 配 IP / 时序方法论 / 工程要可重建 | `references/ip_config_and_timing_closure.md` |
| 新工程首次上板最小闭环 | `templates/minimal_onboard_demo_project.md` |
| 离线全绿、上板无现象 | `prompts/01-offline-green-board-deaf-debug.md` |
| 板上丢样/卡顿/误报（负载类） | `prompts/02-onboard-load-attribution.md` |
| 不开 GUI 程序化出 bit / 审查 BD 改造 | `prompts/03-vivado-tcl-bit-build.md` |
| 交付前后版本/时序/记录校验 | `scripts/` 四个脚本 |
| 上板遇到具体报错 | `pitfalls/` 四篇清单（53 条） |

## When NOT to Use

- Versal / AI Engine（`vai-*`）与分段配置（`segcfg-*`）流程 → 本包不覆盖，用对应 AMD 官方技能。
- 纯 HLS 工具链操作（Vitis IDE 按钮、csim 命令参数）→ 用官方 `hls-*` 技能；本包只给规则与判据。
- 板卡非 Zynq 架构（纯 Artix/Kintex 无 PS）→ 跳过 PS/PYNQ 相关子件，Vivado/HLS 子件仍可用。
- 功能正确性以外的物理实现签核（功耗、热、EMC）→ 不在本包范围。

## DO's

- **先走全流程主线定位阶段，再取子件**：每阶段出口判据不满足不进下一阶段。
- **架构先行**：S1 切分/带宽/资源三张表填满再写代码；HLS 架构阶段禁写 `PIPELINE/UNROLL/PARTITION/INLINE/FLATTEN`。
- **一次只改一个变量**：优化迭代（§HLS 优化循环）与负载归因（P2）同此纪律；失败实验也提交留档。
- **判据优先于现象**：仿真要显式 PASS 标记；时序要 WNS/WHS/结论句三判；板上要四指标 + 与离线基线一致。
- **哈希定版本**：bit 相邻版本字节数可完全相同，只按 MD5/内容特征定版。
- **结论必须可复核**：每个根因给判别实验；每个修复给回归判据；报告按 T2 模板固化。

## DON'Ts

- 不要用"编译通过""仿真正常退出""vlog 0 error"当功能正确证据。
- 不要只用 `report_property` 判 BD 参数生效（内存值会撒谎；save 后 grep 磁盘 `.bd`）。
- 不要在采集回调里做重活（渲染/全量计算/落盘）——板上丢样的历史上游同源坑。
- 不要运行时热改 PL 时钟（写 SLCR `FPGA0_CLK_CTRL` 会把 PL 搞停摆）。
- 不要把硬核 IP（XADC/MIG/PCIe）打包进自定义 IP——硬核一律当 BD 单元格。
- 不要照抄"锁死速率合成源"的停顿阈值（某工程实测 0.5 s 会漏掉 0.384 s 欠账事故）——按最坏欠账自算。
- 不要只判 WNS——hold 违例（WHS）在 setup 全绿时照样存在（实测某版 WNS +2.054 而 WHS −1.862）。

## Workflow

### Step 1: 定位阶段

读 `mainline.md`，对照 10 阶段的"出口判据"确定当前阶段与缺口。
**Verify:** 能说出所处阶段、缺哪张表/哪个判据、下一步入口文件。

### Step 2: 规划（S1）

填 PS/PL 切分表、AXI 带宽预算表、资源频率预估表、时钟复位域表。
**Verify:** 四表无"待定"；资源余量 ≥30%；带宽结论形态明确（安全/需实测/改架构）。

### Step 3: 设计与仿真（S2~S3）

HLS 按设计纪律清单过 5 节；RTL 先 lint 再综合；仿真按契约给显式 PASS。
**Verify:** 可综合性 10 条全过 / lint 0 CRITICAL；仿真五门全过（含 PASS 标记与有限运行）。

### Step 4: 构建（S4~S7）

IP 单 `-dict` + 回读三件套；BD validate 无新增非白名单 CRITICAL；时序 WNS/WHS 双判；bit+hwh 同名同目录 + 哈希台账。
**Verify:** `check_timing.py` PASS；`check_bit_pair.py` 全 `[OK]`；`version_audit.py` 全一致。

### Step 5: 板上验证与排障（S8）

PC 预演 → 上传核对 → 板上分格判活；异常走 P1 三板斧 / P2 负载归因。
**Verify:** 四指标达标；与离线基线按"事件码多重集+卡片簇"一致；产物 `tee` 落盘。

### Step 6: 优化与收尾（S9）

按 HLS 优化循环迭代（有假设/有度量/有记账）；运行期负载用 P2；按 T2 固化复盘。
**Verify:** 每个尝试有"假设→预期→实测→结论"；停止条件明确；报告含判别实验与状态标记。

## Inputs

| Input | Type | Default | Description |
|---|---|---|---|
| 器件/板卡 | 字符串 | 必填 | 如 `xc7z020clg400-1` + PYNQ 板卡型号 |
| 吞吐/延迟目标 | 数值 | 可选 | FPS、S/s、MB/s 或端到端延迟上限（S1 用） |
| 工程目录 | 路径 | 必填 | Vivado 工程或交付目录 |
| bit/hwh 目录 | 路径 | 必填 | 交付产物（同名同目录） |
| 板上记录 | 目录 | 可选 | session 目录（summary + part CSV） |
| timing 报告 | 路径 | 可选 | `*_timing_summary_routed.rpt` |

## Output

- 规划四表（S1）、验证判读表（S3/S6/S7）、板上四指标与复盘报告（S8/S9）。
- 校验脚本输出 `[OK]/[WARN]/[FAIL]` + 退出码，不写包外目录。

## Supporting Files

| 文件（相对本文件） | 用途 |
|---|---|
| [mainline.md](mainline.md) | **主入口**：10 阶段导航、出口判据、官方技能映射 |
| [README.md](README.md) | 22 项子件索引、路线图、通用性分级、五元组约定 |
| `references/` | 6 份规则件：系统规划 / HLS 设计纪律 / HLS 性能优化 / RTL 静态验证 / 仿真契约 / IP 配置与时序方法学 |
| `prompts/` | 3 篇提示词工作流（排障三板斧 / 负载归因 / TCL 出 bit） |
| `templates/` | 4 份案例模板（最小工程 / 运行报告 / 版本核对表 / Jupyter 分阶段） |
| `scripts/` | 4 个校验脚本（bit 配对 / 版本审计 / 记录回放 / 时序判读） |
| `pitfalls/` | 4 篇踩坑清单 53 条（PYNQ 运行 / BD 与 IP / 时钟约束 / HDMI 显示） |

与 AMD 官方技能的分工：官方 `hls-*`/`vivado-*`/`hw-*` 是**完整流程执行器**（带脚本与逐 ID 指南）；本包是其**规则精要 + 闭环纪律 + 板上独有环节**。已装官方技能时优先调用，未装时本包自包含可用。
