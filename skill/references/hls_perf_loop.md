# HLS 性能优化循环

- **类型**：蒸馏件（架构方法 + 迭代优化闭环）
- **适用场景**：① HLS kernel 从"能算"到"能达标"（吞吐/延迟/资源）；② 架构阶段决定宏结构；③ 综合后发现 II 不达标、资源异常、延迟过大。
- **使用方法**：先 §1 定宏架构（此阶段**禁写微优化 pragma**），再 §2 建立基线，按 §3 的 8 步循环迭代；遇到 II 违例走 §4 诊断；有明确吞吐目标时先按 §5 算目标再动手。
- **失效条件**：① 纯 RTL 流程不适用（用 `rtl_static_verification_and_synth_logs.md` + 时序方法学）；② 架构级判据假设"顶层只有一个 DATAFLOW + load/compute/store 三层"，与既有大工程结构不同时只取判据不取目录约定；③ 成本模型的时间量级基于中小规模 kernel，大规模设计按比例放大。
- **已验证效果**：规则源自 AMD 官方技能 `hls-architect`（带 HANDOFF 门控与七件套校验）、`hls-optimize`（8 步循环 + 成本模型 + 停止准则）、`hls-ii-diagnose`（三类根因 + 调度报告取证）、`hls-perf-pragma`（target_ti 级联公式）。这些技能在其官方发布中带版本历史与实战案例。
- **来源**：AMD 官方技能 `hls-architect`、`hls-optimize`、`hls-ii-diagnose`、`hls-perf-pragma`（依据 UG1399、UG902 与 "Parallel Programming for FPGAs"）。

> **官方调用提示**：已安装 `hls-architect`/`hls-optimize`/`hls-ii-diagnose`/`hls-perf-pragma` 时优先调用它们执行完整流程（含脚本与 git 记账）；本文件是规则精要与未安装时的替代。

---

## §1 架构阶段：先定宏结构（禁微优化）

### 1.1 输入与接口先定
- 明确：kernel 源码 + testbench、**part 号**、**时钟周期(ns)**、吞吐目标（可选）。
- **接口选型**（四选一，依据数据形态）：

| 接口 | 适用 | 典型 |
|---|---|---|
| `m_axi` | 大数组走 DDR（帧缓冲、查找表、批处理） | load_input 先整帧读入再计算 |
| `axis` | 实时流（像素/采样流，无缓冲） | 视频/信号处理链 |
| `m_axi + axis` | 一端口流式、另一端口突发读表 | 流输入 + DDR 查表 |
| `m_axi + s_axilite` | 数组走 m_axi、标量参数走控制寄存器 | 独立 kernel 最常见 |

### 1.2 画 activity timeline，选范式
- 列出各处理阶段的时间关系：哪些可并行、哪些串行、数据在阶段间的传递粒度（帧/块/样本）。
- 选范式：**producer-consumer**（块级缓冲、生产消费解耦）或 **streaming**（逐样本/逐像素直通）。判据：端到端延迟要求紧、无全局依赖 → streaming；需要整帧统计/多遍处理 → producer-consumer。

### 1.3 固定三层顶层结构
```
load_input(...)  →  compute(...)  →  store_output(...)
```
- 顶层**只有一个** `#pragma HLS DATAFLOW`；compute 内部再分解（分层调用 or 单块），由 dataflow 规范化判据（见 `hls_design_discipline.md` §3）裁决。
- 接口 pragma 只写在顶层三个函数的入参上。

### 1.4 架构阶段的硬纪律
- **禁用**：`PIPELINE`、`UNROLL`、`ARRAY_PARTITION`、`INLINE`、`FLATTEN`（违反即删除）——宏架构未定就微优化会锁死结构。
- 性能意图只写 `#pragma HLS performance target_ti=N`（见 §5），由工具分配。
- 配置键：`part=` 为顶层键（`syn.part=` 非法）；`clock=` 用 ns（`syn.clkPeriod=` 非法）。
- 不把库头文件链进用户设计；不把 `INTERFACE` 写进子函数；架构阶段不跑 csynth。

### 1.5 出口门控（HANDOFF）
每步结束必须能回答：产出物是什么 / 满足什么条件才能进下一步 / 不满足时阻塞在哪。**上一阶段条件不满足，禁止进下一阶段。**
七件套校验（全过才交接）：① 可综合性 10 条 ② pragma 位置合法 ③ dataflow canonical（若用）④ g++ 功能验证通过 ⑤ 接口 pragma 仅顶层 ⑥ 架构阶段无被禁 pragma ⑦ 吞吐目标已转成 target_ti 或明确"无目标"。

---

## §2 建立基线（任何优化之前）

记录四项：① cosim 周期数 ② 布线后时钟周期(ns) ③ **真实延迟 = 周期 × 周期时间** ④ 资源（LUT/FF/DSP/BRAM/URAM）。
基线要求：csim/csynth/cosim/impl 全部成功且报告不过期；任一缺失先跑对应流程。**基线不建立，后续所有比较无效。**

---

## §3 迭代循环（一次只改一个变量）

### 3.1 八步
1. **读报告找瓶颈**（四类信号）：内层高 II → 存储端口或长递推；未流水循环的迭代延迟大 → 缺 pipeline 或不可流水；资源利用率极低（DSP 2%）→ 可面积换性能；外层串行包内层流水 → 内层流水每次重启是主延迟项。
2. **写假设**：改什么 / 预期报告怎么变 / 什么能证实 / **什么能证伪**（没有证伪条件的实验不做）。
3. **选一个技术**：pragma 类（II 认领、部分/完全展开、数组分区 cyclic/block/complete、flatten、dataflow、expression balance）优先；算法类（循环重排、分块、局部缓冲、标量化、融合/分裂、定点化、除法改倒数乘）风险高收益大。
4. **实施**：只做描述的那一个改动，不顺手修别的。
5. **查结果（先查 pragma 是否被忽略）**：在综合日志搜 `pragma|ignored|cannot|not applied|warning`——被忽略时结果反映的是**未修改**的调度，此时看报告会得出错误结论。常见被忽略模式：`PIPELINE ignored`（循环不合格）、`UNROLL factor=N ignored`（边界不可静态确定/不整除）、`ARRAY_PARTITION ignored`（非局部数组/维度越界）、`LOOP_FLATTEN ignored`（边界非循环不变）、`II target cannot be achieved`（看违例报告定根因）。
6. **系统排查**：循环表看 II achieved vs target、迭代延迟、资源；查 `inferred_directives`（工具自动推断会与手写 pragma 打架）；用 QoR 历史脚本记账（`qor_log.py --csv`）。
7. **记账 + 提交**：**成败都提交**（失败实验先 `commit --allow-empty` 留档，再回滚；三重确认：`git log -1` + `git status --short` + 提交信息写清假设）。
8. **决定是否升级流程**：见成本模型。

### 3.2 成本模型（升级门）
| 流程 | 单次耗时 | 何时跑 |
|---|---|---|
| csim | 5–30 s | 每次改动 |
| csynth | 10–60 s | 每次改动（内循环主力） |
| cosim | 30 s–5 min | **仅当 csynth 改善 ≥10%**（或结构性变化如 II 下降） |
| impl | 1–30 min | **仅当 cosim 确认改善** |

**昂贵步骤是门，不是橡皮章**。

### 3.3 停止准则（任一满足即停）
连续 3 次尝试无改善；任一受限资源 >60%；单次边际收益 <5%。

### 3.4 禁止事项
禁止直接用文件工具读 `.rpt`（走报告技能/解析脚本）；禁止一次改多个技术；禁止用扫射式 `PIPELINE II=<N>` 代替诊断（见 §4）。

---

## §4 II 违例诊断（从"猜"变"证"）

**触发**：csynth 后某循环/函数 `II achieved > II target`，或无 target 时 `achieved > 1`。

### 五步
1. **取报告与原始日志**。注意：汇总表里的违例标签很粗（"Memory Dependency" 常与存储端口无关）；真证据在原始日志 `logs/hls_compile.log` 的 `Pipelining loop/function ...` 到 `Pipelining result ...` 块（这些是 `INFO:` 行，被过滤后的日志会丢掉）。
2. **抽流水块**：`awk '/Pipelining (loop|function)/{f=1} f; /Pipelining result/{f=0}'`；读全块（携带变量名/类型、距离、生产者/消费者行号 `HLS 200-880`、时序 `HLS 200-887`、资源限制消息），不要只看 result 行。
3. **在调度报告里确认长延迟元素**（递推类必做）：`hls/.autopilot/db/<module>.verbose.sched.rpt`，找 `ST_n : Operation k [20/20] (1.18ns) ---> "…" [file:line]` 行——**跨阶段最多/延迟最大的那个 op 就是长延迟元素**，它直接告诉你修法方向（`call` → 内联；`load/store` → 降访存延迟；长算术链 → 重构）。不要从源码推断。
4. **分类**：先分"递推依赖"还是"资源不足"，再落到具体类：
   - (a) 未设 II 目标（时序优先，工具自动放宽 II）；
   - (b) 递推路径长延迟（未内联调用 / 访存 / 共享 mux / 长组合链）；
   - (c) 单迭代内存储端口不足（非跨迭代依赖）。
   多个原因可并存，**全部报告并说明谁是物理限制、谁是放宽原因**。
5. **定位并引用证据**：综合日志行、调度报告 `ST_n … Operation … (delay)` 行、源码行。每条结论必须可被他人独立复核。

**DON'T**：靠汇总表标签定根因；用 `PIPELINE II=N` 扫射代替诊断；只报一个原因而忽略并存原因。

---

## §5 吞吐目标 → target_ti 换算（写 pragma 之前）

### 5.1 采集输入
时钟周期 `clock`（ns）→ `clock_hz = 1e9 / clock`；架构类型（单函数 / 顺序函数 / dataflow）；循环层数与 trip count；吞吐目标三种互转：
- **FPS / invocations per second**：直接用。
- **数据率 MB/s 或 GB/s**：`invocations/s = 数据率(字节/s) ÷ 每次调用输入字节数`。
- **GOPS/GFLOPS**：`invocations/s = ops/s ÷ 每次调用操作数`（1 MAC = 2 FLOP）。

### 5.2 顶层公式
```
target_ti = clock_hz / throughput_rate        # 单位：周期
```

### 5.3 架构级联
- **单函数**：全额施加。
- **顺序函数**：总 latency 为**求和**，按 trip count 比例分配预算；先加顶层 pragma，综合确认瓶颈后再加函数/循环级。
- **dataflow（含 `hls::task`）**：吞吐由**最慢 stage** 决定 → 顶层 `target_ti` 是**每个 stage 的上限（同值，不是分数）**；某 stage 分配值超过顶层值就会主导吞吐并导致 miss。

### 5.4 循环级联
```
内层 target_ti = 外层 target_ti / 外层 trip_count
（逐层向下除）
```
**trip count 是运行期参数或输入相关 → 立即停手**，先跑 csim 拿到真实迭代次数，禁止猜。

### 5.5 pragma 位置
- 函数级：`#pragma HLS performance target_ti=N` 写在**顶层函数体第一条**（Position 1）。
- 循环级：写在**循环体第一条**（Position 2），或循环标签与 `for` 之间亦可；**只有顶层函数可加函数级 pragma，其余一律落循环**。

### 5.6 校验清单
`target_ti` 拼写正确（不是 `target_ii`）；数值为正整数；位置符合 5.5；表里每个目标都有对应 pragma。

---

## 交叉引用

- 规则合法性（可综合性/pragma 位置/dataflow/pipeline/burst）→ `hls_design_discipline.md`
- 板上运行期性能（丢样/负载挤占）→ `../prompts/02-onboard-load-attribution.md`
- 资源/时序收口 → `rtl_static_verification_and_synth_logs.md`、`../scripts/check_timing.py`
