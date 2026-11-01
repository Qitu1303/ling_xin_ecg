# RTL 静态验证与综合日志处置

- **类型**：蒸馏件（lint 闸门 + 综合消息处置）
- **适用场景**：手写 Verilog/SystemVerilog/VHDL 的工程：改完 RTL 早期自检（lint）；综合报错/告警后的逐条定位与修复；接手他人 RTL 的第一轮体检。
- **使用方法**：§1 先跑 lint（廉价闸门），通过后再进综合；§2 处理 `[Synth 8-XXX]` 消息（报告先出、源码后改）；§3 是常见规则的处置取向表。
- **失效条件**：① lint 只能查"静态可判"的问题，查不出功能错误（那要靠仿真契约）；② `[Synth 8-XXX]` 编号与文本随 Vivado 版本变化，处置原则稳定但具体编号以本机日志为准；③ 官方技能依赖 Vivado MCP / 特定版本，本文件已改写为 batch TCL 可用口径。
- **已验证效果**：规则源自 AMD 官方技能 `vivado-rtl-lint`（含 20 份逐 ID 修复指南与"禁直读 .rpt"的解析纪律）与 `vivado-rtl-elaboration-analysis`（覆盖 ~400 个 8-XXX ERROR + 86 WARNING + 8 CRITICAL WARNING 的逐条解析，含 11 项准确率校验）。两条流程在官方发布中均带版本历史与回归用例。
- **来源**：AMD 官方技能 `vivado-rtl-lint`、`vivado-rtl-elaboration-analysis`（依据 UG901 综合指南、UG906 设计分析、UG949 UltraFast 方法学）。

> **官方调用提示**：已安装 `vivado-rtl-lint` / `vivado-rtl-elaboration-analysis` 时优先调用（带完整逐 ID 修复指南与解析脚本）；本文件是流程纪律与高频规则的精要。

---

## §1 lint 闸门（综合前 1~5 分钟买保险）

### 1.1 流程（batch TCL 可跑，不依赖 GUI/MCP）
1. **判工程形态**：有 `.xpr` 走工程模式（读 part 与 top）；无 `.xpr` 走非工程模式——glob `.v/.sv/.vhd`、递归收集 include 目录、按扩展名选 `read_verilog -sv`、`update_compile_order` 后 `find_top`。**多个 top 候选必须问用户**，不许猜。
2. **建独立报告目录**（如 `vivado_agentic_ai_reports/rtl-lint/`），不要在工程根散落。
3. **跑 lint**：`synth_design -top <top> -part <part> -lint -file <报告路径>`；确认文件存在且非空。
4. **转 CSV（强制）**：`.rpt` 是 ASCII 表格（字段里可能有字面 `|`、多行单元格），**禁止直读**；用官方 `parse_lint_report.py`（Vivado < 2026.1）或原生 CSV（≥ 2026.1）转成 7 列：`规则ID / 描述 / 对象名 / 附加信息 / 模块 / 文件 / 行号`。
5. **统计一致**：CSV 行数必须等于 Vivado 报的 `Total of X linter message(s)`。**X=0 就报 0 条，绝不臆造**。
6. **出报告**：按规则 ID 分组 + 按文件热点分组；CRITICAL 逐条给修复（读原始源码 ±5 行，diff 语法，真实代码不编造）；WARNING/INFO 按数量分级（≤50 全列；51–200 列 CRITICAL/WARNING、INFO 抽样；>200 只全列 CRITICAL）。

### 1.2 纪律
- 先修 CRITICAL，再 WARNING，最后 INFO（INFO 也常影响 QoR，别忽略）。
- 注释不是代码：`endmodule // xxx` 里 `//` 之后的内容不算违规。
- 大批量同型告警（如 VHDL 全寄存器 RESET-2）评估用 waiver + 理由，而非逐条改。
- 每次修复后**重跑 lint**，直到计数一致。

## §2 综合日志处置（`[Synth 8-XXX]`）

### 2.1 找日志（优先级）
`<run>/synth_1/runme.log` > `vivado.log` > 其他 `.log`；无日志才走在线 session。

### 2.2 流程（七步，顺序不可乱）
1. **日志→CSV**：抽 `^(ERROR|CRITICAL WARNING|WARNING):\s*\[Synth\s+8-\d+\]`，7 列：序号/严重级/消息ID/消息/文件/行号/语言。
2. **建 resolution 清单**：唯一 `msg_id`（排除 INFO）逐个标注"有专门处置指南/无（走通用指南或自行分析）"，两栏必须覆盖全部 ID。
3. **读真实 RTL**：每条消息都要定位到真实文件与行号，不得凭消息文本猜代码。
4. **写报告（先于改码）**：每个非 INFO 消息一个 `###` 节：文件链接 + 问题代码 diff + 建议修复 diff（可直接粘贴的整段代码）+ 明确"粘到哪个文件第几行"。
5. **准确性校验（硬停）**：节数 = 非 INFO 消息数；每节 occurrence 总数与 CSV 一致。**不一致不许进入改码**。
6. **才允许改 RTL**：按 ERROR → CRITICAL WARNING → WARNING 顺序。
7. **重跑综合**验证，直到剩余消息全部为 INFO 或有明确豁免理由。

### 2.3 纪律
- **报告先出、源码后改**——先改码会让 diff 里的"原始代码"失真。
- 级联消息（由同一根因引发）也要建节，可一句话注明"由 8-XXX 修复解决"。
- 禁止捏造 RTL 或捏造违例；禁止把"综合器没报"当成"没问题"。
- 报告目录放**用例/工程本地**，不建工作区根级共享目录（多人/多版本会串）。

## §3 常见规则处置取向（按症状查）

| 症状/规则族 | 典型含义 | 处置方向 |
|---|---|---|
| ASSIGN 系（位未赋值/多驱动/宽度不匹配） | 组合逻辑不完整或位宽截断 | 补全赋值分支、统一位宽、避免同一信号多 always 驱动（会综合出 multi-driven net） |
| INFER 系（推断出锁存器/不完整 case） | 组合 always 缺 else/default | 补 `default`，或在时序块里写全分支 |
| CLOCK 系（时钟用法不当） | 时钟当数据用、门控时钟 | 改用时钟使能；跨时钟域用同步器 |
| RESET 系（复位缺失/异步复位用法） | 寄存器无复位或复位极性混用 | 统一复位策略；大批量时评估 waiver |
| QOR 系（综合质量提示） | 扇出过大、逻辑层级过深 | 拆分逻辑、加寄存器、评估流水 |
| 8-XXX 常见 ERROR 族 | 端口不存在/类型不匹配/重复定义/参数越界 | 以 `.veo`/`.xci` 权威清单核对端口；`vlog` 通过不等于 elaborate 通过（见 `../pitfalls/vivado_bd_and_ip.md` K2-10） |

**两个高频实战教训**（与 `../pitfalls/vivado_bd_and_ip.md` 呼应）：
- `vlog` 只做语法检查，具名端口连到不存在的端口**不报错**——端口正确性要在 elaborate/xsim 层验证。
- 生成的 AXI 从机模板里"读选择器"在用户逻辑区之前，新信号声明必须上提，否则仿真报 `Undefined variable`。

## §4 与 S6 出口判据的衔接

lint 0 CRITICAL ∧ elaborate 无 ERROR ∧ 剩余 WARNING 均有处置或豁免理由 → 才进入时序收敛（`../scripts/check_timing.py` + `../pitfalls/clock_and_constraints.md`）。

## 交叉引用

- 模块级功能验证 → `simulation_verification_contract.md`
- IP 参数化与 BD 集成 → `ip_config_and_timing_closure.md`、`../pitfalls/vivado_bd_and_ip.md`
- 时序与 DRC（VCCO/PDRC/BUFR）→ `../pitfalls/clock_and_constraints.md`
