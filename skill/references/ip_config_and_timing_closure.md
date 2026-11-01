# IP 配置与时序方法学闭环

- **类型**：蒸馏件（IP 参数化纪律 + 时序方法学 + 可重建工程）
- **适用场景**：① 用 TCL/Agent 配置任意 Vivado IP（AXI 外设、PS7、DMA、时钟、复位、互连）；② 实现后 `report_methodology` 出违规；③ 工程要交接/进版本库/CI 重建。
- **使用方法**：§1 配 IP（先台账、再单 `-dict`、必回读）；§2 时序方法学（分组→逐组修复→复验循环）；§3 可重建工程五步。
- **失效条件**：① 官方 IP 配置技能依赖 Vivado MCP 与文档检索 MCP，本文件改写为"规则 + batch TCL"口径，不含 137 个 `ipcfg::*` 过程本体；② 时序方法学检查 ID 与参考文档随版本变化，处置原则稳定、具体 ID 以本机 `report_methodology` 输出为准；③ 可重建步骤对 DFX/BDC/SegCfg 有额外分支，本文件只保留通用主干。
- **已验证效果**：规则源自 AMD 官方技能 `vivado-ip-configurator`（含 `lib/ipcfg.tcl` 3606 行、137 个过程、需求台账与覆盖率披露机制）、`vivado-timing-methodology-checks`（`SUPPORTED_IDS.md` 实测 57 个 TIMING ID + 10 份逐 ID 解析 + AUTO/USER 分流）、`vivado-revision-control`（5 步流水线 + `build.tcl` 生成，覆盖标准/DFX/BDC/SegCfg 四类工程）。
- **来源**：AMD 官方技能 `vivado-ip-configurator`、`vivado-timing-methodology-checks`、`vivado-revision-control`（依据 UG906 设计分析与收敛、UG835 Tcl 命令参考、UG949）。

> **官方调用提示**：已安装上述三个技能时优先调用（IP 配置技能带完整参数库与错误恢复；时序技能带逐 ID 参考文档；版本控制技能带 `helper_scripts.tcl`）。本文件是纪律与判据精要。

---

## §1 IP 参数化：台账 → 单 dict → 回读

### 1.1 需求台账（先拆需求，再动 Tcl）
把一句话需求拆成原子条目，每条记录：`IP 类型 / 参数语义 / 期望值 / 期望的可见变化（默认值是什么）`。复合短语要拆开（"带 4 个通道的 DMA" = 模块类型 + 通道数两条）。台账是后续覆盖率披露的依据——**没进台账的需求等于没做**。

### 1.2 文档定键（不猜参数名）
- 每个参数先查 IP 文档确认 `CONFIG.*` 键名与合法值域；**按描述选，不按名字像**（同名相似参数极常见）。
- 一个需求往往由**多个兄弟参数**共同实现，要枚举完整邻域，只设一个会静默欠配置。
- 命中过的"需求短语 → 参数"映射可缓存复用（带文档出处与 IP 版本）。

### 1.3 一次 `-dict` 设置，原子回滚
```tcl
set_property -dict [list CONFIG.X {1} CONFIG.Y {0}] [get_bd_cells <cell>]
```
- **禁止逐参数 `set_property`**（失败无法原子回滚）；**禁止先 dump 全参数空间**（除关键词邻域发现）。
- 一个 IP 累计尝试 ≤10 次；VLNV 不写版本号。

### 1.4 回读校验三件套（BD 模式的致命坑）
**BD 模式下未知 CONFIG 键不抛错**：只发非致命 `CRITICAL WARNING [BD 41-1276] ... does not exist`，且 `catch` 返回 0。因此必须：
1. **`verify_stuck`**：set 后回读值，确认"真的设进去了"（不是被门控/忽略）；
2. **`verify_intent`**：逐条核对台账里的"期望可见变化"是否出现；
3. **`find_disabled`**：列出该 cell 上处于 disabled 的参数量，确认没有命中你的目标参数。
- `SUCCESS:` 行不足以判成功；三者皆空/全绿才算 applied。
- 参数被门控时的处理见 `../pitfalls/vivado_bd_and_ip.md` K2-1（`BD 41-721`）。

### 1.5 结局与覆盖率披露
每个原子需求给唯一归宿：`applied` / `default`（默认就满足）/ `unapplied:<原因>`。原因枚举：`runtime-register-only`（只能运行期写寄存器）、`integration-derived`（由集成方式推导）、`gated-no-enabler`（门控且无总闸）、`not-a-config-param`（不是配置参数）、`value-out-of-range`（值域外）、`wrong-ip-for-capability`（该 IP 不具备此能力）。
**必须向用户逐条披露未应用项**（原文短语 + 原因），禁止把"部分达成"说成"已配置"。

### 1.6 错误恢复（Tier 2）
`-dict` 抛错时：解析错误消息 → 查文档定位被拒参数 → 剥离只读/非法参数后重建 `-dict` 重试（≤10 次）→ 仍失败则退到 standalone `create_ip` 验证，隔离"BD 环境问题 vs 参数问题"。

### 1.7 输出卫生
成功的工具响应保持精简（约 1 KiB 内）；完整 CONFIG 快照/依赖树写证据文件而非打印；证据文件只读不改、每次新文件名；大报告用 `verbosity=digest` 并保留日志路径。

---

## §2 时序方法学闭环（`report_methodology`，57 个 TIMING ID）

### 2.1 十步闭环
1. **打开设计**：`open_checkpoint <dcp>`，或工程 `open_project` + `open_run <run>`。
2. **跑报告**：`report_methodology -json methodology.json`；提取字段 `RULE`（如 TIMING-1）、`NAME`（如 TIMING-1#1）、`SEVERITY`、`DETAILS`、`NETLIST_ELEMENT`（cell/pin 层级名）。
3. **导出约束并回溯来源**：`write_xdc -force -exclude_physical constraints.xdc`；合并文件里按 `# Constraints from file : '<原名>'` 注释还原**原始文件名 + 行号 + 约束类型 + 对象 + 值**。
4. **判定是否支持**：查 `SUPPORTED_IDS`（GROUP / PRIORITY / 是否有逐 ID 参考文档）；不支持的 ID 走通用指南。
5. **分组排序**：GROUP 1 最高优先，组内按 PRIORITY 升序；此阶段只排序不修。
6. **生成数据**：只对当前组的 ID 采集参考文档要求的证据，不跨组、不重复采。
7. **按组修复**：`AUTO`（可自动修，如改约束写法）直接执行；`USER`（涉及设计意图）**禁止猜**，先展示命令再问 `[Y/n]`。修前在 agent 日志里确认四问：找到问题项 / 理解为何是问题 / 理解正确修法及理由 / 与备选方案比较。
8. **每组后复验**：`reset_timing` → `read_xdc` → 重跑 `report_methodology`；不复验等于没修。
9. **循环到全清**（或剩余项全部有豁免）。
10. **出报告**：`METHODOLOGY_RESOLUTION_REPORT.md`，含改动、依据、剩余违例与豁免。

### 2.2 豁免纪律
waiver 必须留 `user / date / reason` 三元组；理由写"为什么可以接受"，不写"暂时不管"。

### 2.3 与自有脚本的分工
- 本闭环管"方法学规则"（约束写法、时钟结构、CDC 风格）；
- 物理收敛（hold 违例、BUFR 跨域偏斜、pblock）用 `../pitfalls/clock_and_constraints.md` K3-6 的实测序列 + `../scripts/check_timing.py` 判读；
- 两者结论冲突时：以 `report_methodology` 的规则依据 + 实测时序数字共同判定，不以单一报告下结论。

---

## §3 可重建工程（交接/CI/版本库）

### 3.1 五步流水线
1. **识别工程类型**：标准 RTL / DFX / 分段配置 / 含 BDC（Block Design Container）——类型决定后续导出策略。
2. **分析源码位置**：全部本地（push-button）/ 全部外部（remote）/ 混合。**remote 场景跳过导出**，避免把外部仓库复制进本工程。
3. **导出源码**：`export_all_sources`（把 IP、BD、约束、仿真文件组织进自包含目录）；可选保留指定 IP/BD 的生成产物（`.gen`），否则全部由 Tcl 重建。
4. **抓取设置**：part、top、编译顺序、IP 版本、约束集、仿真设置等工程属性导出为可重放脚本。
5. **生成 `build.tcl`**：一条命令从空目录重建工程（建工程→读源→设属性→生成 IP/BD→可选综合）。

### 3.2 判据与实践
- 重建验证是唯一判据：在干净目录跑 `build.tcl`，产物与原件一致（bit 以 MD5 比对，见 `../scripts/check_bit_pair.py`）。
- 大工程首次建 git 的忽略清单要按体积复核（`*` 不跨目录，图片/大文件要 `**/*.img` 这类模式）。
- 与自有的"双树 + 哈希台账"配合：源头可重建（本文件）+ 交付包哈希一致（`../templates/deployment_version_checklist.md`）。

## 交叉引用

- BD 拓扑与孤儿网排障 → `../pitfalls/vivado_bd_and_ip.md`
- 时序/DRC 物理收敛 → `../pitfalls/clock_and_constraints.md`、`../scripts/check_timing.py`
- bit 交付与版本目录 → `../prompts/03-vivado-tcl-bit-build.md`、`../templates/deployment_version_checklist.md`
