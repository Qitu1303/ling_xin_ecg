# HLS 设计纪律清单

- **类型**：蒸馏件（规则清单，来自 AMD 官方 HLS 技能族）
- **适用场景**：用 C/C++ 写 HLS kernel（Vitis HLS / 任何 HLS 流程）；代码评审；AI 生成 HLS 代码后的自检。
- **使用方法**：按 §1→§5 顺序过一遍：先判"能不能综合"，再判"pragma 放对没有"，再判"结构合不规范"，最后看流水/突发配置。每节末有判定口径。
- **失效条件**：① 纯 RTL（Verilog/VHDL）设计不适用本清单，改用 `rtl_static_verification_and_synth_logs.md`；② 各条判据基于 Vitis HLS 2024+ 行为，旧版本个别规则（如 `hls::task` 的 `hls_thread_local` 要求）可能不同，以本机版本日志为准；③ 本清单只判"规则是否满足"，不判"算法是否正确"（那是仿真契约的事）。
- **已验证效果**：规则源自 AMD 官方技能中的验证过判据——`hls-synthesizable` 的 10 条语言层规则、`hls-pragma-scope` 的 4 类放置规则、`hls-dataflow` 的 10 组规范化判据、`hls-pipeline` 的 II/rewind/style 规则、`hls-burst-inference` 的 9 前置 + 5 限制；这些技能在其官方发布中带版本历史与回归用例。
- **来源**：AMD 官方技能 `hls-synthesizable`、`hls-pragma-scope`、`hls-dataflow`、`hls-pipeline`、`hls-burst-inference`、`hls-flattenable`、`hls-array-to-stream`（依据 UG1399 Vitis HLS 用户指南与 "Parallel Programming for FPGAs"）。

> **官方调用提示**：已安装对应官方技能时，优先用 `use_skill` 调用它做完整判定（它们带脚本与逐条裁决）；本清单用于快速自查与未安装环境。

---

## §1 可综合性 10 条（语言层硬闸门）

**判定范围**：只查 top 函数及其调用链；**不查 testbench、不查 `main()`**。

| # | 规则 | 判据 |
|---|---|---|
| 1 | 系统调用须守卫 | `printf/fprintf/getc/time/sleep/cin/cout` 等必须在 `#ifndef __SYNTHESIS__ … #endif` 内（其他宏守卫也算合法，但建议改用 `__SYNTHESIS__`） |
| 2 | 动态内存须守卫 | `malloc/alloc/free/new/delete` 同上 |
| 3 | 禁虚函数 | 出现即不可综合 |
| 4 | 禁递归 | 调用链上出现自调用即不可综合 |
| 5 | STL 白名单 | 仅允许数学函数族（`std::abs/sqrt/sin/exp/pow/…`）、`std::complex` 及其运算、比较器（`equal_to/less/greater…`）；名单外的 STL 一律禁止 |
| 6 | 禁 `long double` | 出现即不可综合 |
| 7 | 禁函数指针 | 出现即不可综合 |
| 8 | 禁虚函数指针 | 出现即不可综合 |
| 9 | 禁二级指针 | `T**` 出现即不可综合 |
| 10 | 任意精度类型禁指针转换 | `ap_int/ap_uint/ap_fixed/ap_ufixed/ap_float/half` 与原生类型之间、任意精度类型之间，**都不允许指针转换**（值转换可以） |

**判定**：10 条全过 = "可综合子集"；任一不过 = 不可综合（先修语言层，别急着调 pragma）。

---

## §2 pragma 放置 4 类（放错 = 完全不生效）

**核心规则**：pragma 作用于**最近的 `{}` 块**。写在 `{` 之前或 `}` 之后 → **不生效**（最常见的静默失效）。

### A 函数体（必须直接位于函数体内）
`TOP`、`INLINE`、`INTERFACE`、`FUNCTION_INSTANTIATE`、`STABLE`；`PIPELINE`/`DATAFLOW`/`DEPENDENCE` 为双域（函数体与循环体皆可）。
- **`INTERFACE` 只在 top 函数体内合法**；写进子函数会被忽略；`port=` 必须引用 top 参数。
- `INLINE region` 与 `INLINE` 语义不同：前者内联区域内所有调用，后者内联"所在函数"。

### B 循环体（必须直接位于循环体内）
`UNROLL`、`LOOP_FLATTEN`、`LOOP_TRIPCOUNT`；`PIPELINE`/`DATAFLOW`/`DEPENDENCE` 双域。
- `UNROLL` 只展开**紧邻**循环，写外层不展开内层。
- `LOOP_FLATTEN off` 双向隔离：本层既不与内层合并，也不与外层合并；更深的层之间仍可合并。
- `LOOP_TRIPCOUNT` 只对**变量边界**循环有意义，且只影响报告、不影响综合。

### C 区域（任意 `{}` 块，含裸块）
`ALLOCATION`、`EXPRESSION_BALANCE`、`LOOP_MERGE`、`LATENCY`、`OCCURRENCE`。
- `LOOP_MERGE` 必须放在**包含**待合并循环的那一层（父函数或外层循环体），不能放进被合并的循环里。
- `OCCURRENCE` 必须在流水化 scope 内的**条件块**里。

### D 变量（变量必须在当前位置可见）
`ARRAY_PARTITION`、`ARRAY_RESHAPE`、`BIND_STORAGE`、`BIND_OP`、`STREAM`、`ARRAY_STENCIL`、`DISAGGREGATE`、`AGGREGATE`。
- **`STREAM` 不能用于 top 函数入参** → 改用 `INTERFACE mode=ap_fifo/axis`。
- 变量 pragma 会**跨调用传播**（调用方变量上的 pragma 传播到被调函数的对应形参）。

### 两条互斥/隐式规则（最高频踩坑）
1. **`DATAFLOW` 与 `PIPELINE` 在同一 scope 互斥**，不能同时作用于同一个函数体或循环体。
2. **`PIPELINE` 会隐式完全展开其下所有子循环**；若某个子循环不可展开（如变量边界），外层 `PIPELINE` 会被**忽略**并在日志中报告。

---

## §3 dataflow 规范化判据（canonical 才可预测）

**判定前提**：代码含 `#pragma HLS dataflow`（或 `dataflow disable_start_propagation`），且位于函数体或循环体。

### 3.1 循环型 dataflow 的 6 条（pragma 在循环体时）
1. 必须是 `for` 循环；2. 父函数**只含**该循环（外加局部变量声明与 pragma）；3. 计数器在循环头声明且为 `int`；4. 初值为非负常量；5. 边界为非负常量或**标量入参**；6. 步长为正常量。

### 3.2 区域内容 8 条（逐行查）
1. 区域内只允许：pragma、局部变量声明、函数调用（三类之外一律不规范）。
2. `hls::task` 必须声明为 `hls_thread_local`。
3. `hls::task` 内的 `hls::stream`/`stream_of_blocks` 也必须 `hls_thread_local`。
4. 局部变量必须**非 static**。
5. **带默认构造的局部变量（如 `std::complex`）会隐式生成写进程** → 加 `__attribute__((no_ctor))`。
6. **调用点不得传数组元素**（`mem[i]`、`arg[offset]`）——只能传整个数组/指针变量。
7. **调用点不得做算术/逻辑运算**（传 `i` 不传 `i+1`，传 `ptr` 不传 `ptr+offset`）。
8. 调用点**禁类型转换**（唯一例外：值→引用）。

### 3.3 入参/全局/局部端口规则（摘要）
- `hls::stream` 入参：**只允许一个进程访问**。
- m_axi 数组入参：单进程访问；或"先读进程后写进程"（文本顺序）。
- 非 m_axi 数组入参：写→单进程；只读→最多两个进程。
- 全局/static 数组：区域内**最多一个进程**读写。
- 局部 `hls::stream`：一写一读；`stream_of_blocks`：一写一读且必须 `write_lock`/`read_lock`。
- 局部数组：`ram_1wnr` 类型 = 一写多读（写者在前）；普通数组 = 一写一读（写者在前）。

**判定**：全部满足 = canonical dataflow；否则工具会插入隐式分组/提取循环，性能与结构不可预测。

---

## §4 pipeline 规则（合法性 / II / rewind / style）

### 4.1 合法性（不满足就别写 pragma）
- **函数级**：该函数在同一并行域被调用 >1 次；其下所有循环可完全展开；无冲突 pragma（函数/循环 DATAFLOW、LOOP_FLATTEN、部分展开、UNROLL off、循环 PIPELINE）。
- **循环级**：其下所有子循环可完全展开；无冲突 pragma（函数/循环 DATAFLOW、LOOP_FLATTEN、部分展开、UNROLL off、嵌套 PIPELINE）。

### 4.2 II 选择
- **从 `II=1` 起试**；不达标就读 target II 失败消息（三类原因：跨迭代依赖、IO/存储端口不足、时序撑不住）再逐级退让。
- 不写 `II` 时，工具按"时序优先"自选 II。
- **II 与时钟周期可交换**：放宽时钟周期（降频）可让两周期操作合一，从而降低 II。

### 4.3 rewind
- **默认不写**，交给 auto（工具按周围上下文判断，比人可靠）。
- 仅在"已验证必须强制"时写 `rewind=true/false`；**只对循环合法，函数级写 rewind 直接报错**。
- 需要 rewind 的结构签名：R1 循环所在函数在同一并行域被多次调用；R2 循环在重复执行的外层循环下；R3 循环在重复进入的 dataflow 区域内。都不满足时 rewind 无收益。

### 4.4 style
- `stp`（默认，非 flushable）：**在 dataflow 区域内且存在阻塞 I/O**（stream 读写、`ap_hs`/`ap_fifo`、可能等待的 m_axi）时会**死锁** → 必须换 `flp`/`frp`；无阻塞 I/O 时 `stp` 资源最省、首选。
- `flp`（flushable）：停顿时可在途数据继续排出，避免死锁；II 可能变大。
- `frp`（free-running）：从不 stall，每周期都跑，需要至少一个阻塞 I/O；**控制流复杂（条件 `break/continue` 且条件不可化简）时选它**，可降扇出、改善时序。
- 不写 `style` 时工具自动选（`hls::task` → flp；高扇出且满足条件 → frp；否则 stp）。

---

## §5 burst 推断（m_axi 带宽优化）9 前置 + 5 限制

### 前置（全局 4 条 + 区域 5 条）
1. 变量必须是 m_axi 顶层实参（Vivado flow 下无 interface pragma 则**不是** m_axi；Vitis flow 下默认是）。
2. DATAFLOW 只作为**循环块的直接语句**时才禁止 burst；在嵌套块内或循环外都不影响。
3. 访问数据类型位宽必须是 **2 的幂**（有 `aligned` 属性按对齐后大小算）。
4. 变量**不能带 `volatile`**。
5. burst 长度 ≥2（长度小不构成拒绝理由）。
6. 访问必须**连续、单调递增、无间隙无重叠**；只访问子区间允许。
7. 长度/步长必须在请求发出前可确定（**允许运行期计算**，不要求编译期常量）。
8. 同一 bundle 同一 channel 上**不得并存同类同向访问**（多个 m_axi 口都在读/都在写 → 全都不 burst）。
9. 请求发起至完成期间**不得有地址依赖**（`a[i] += 5`、`b[i]=a[i]; a[i]=c[i]` → 不 burst）。

### 可能失败的限制（前置满足也可能不 burst）
1. `ap_int/ap_uint` 作循环归纳变量。
2. dataflow 内多个任务共享同一 m_axi 口（同 port/bundle）。
3. 循环间/循环内依赖。
4. 跨函数传索引且子函数 `inline off`（`inline` 时可行）。
5. 潜在地址重叠（读写可能落到同一地址，除非能证明 `i≠j`）。

**判定**：前置全过且无命中限制 = 可 burst；否则不可。**对 Zynq-7000 尤其重要**：PS DDR 经 HP 口访问时，burst 与连续访问对带宽影响最大（见 `system_planning_pspl_split_and_bandwidth.md`）。

---

## §6 flatten / array→stream 判据（摘要）

- **可 flatten**：嵌套循环边界为常量或标量入参、循环体无跨层依赖、无数据相关退出；flatten 后配合最内层 `PIPELINE` 可把多层循环合成一条统一流水线。
- **array→stream**：数组只被顺序访问、无随机索引、无跨迭代回读 → 可改 `hls::stream`；一旦存在随机访问或多次回读，改 stream 会破坏语义，不要强改。

## 交叉引用

- 架构阶段（先定宏架构再微优化）→ `hls_perf_loop.md` §1
- II 违例根因诊断 → `hls_perf_loop.md` §4
- 综合日志里"pragma 被忽略"的处置 → `hls_perf_loop.md` §3
- 手写 RTL 的静态检查 → `rtl_static_verification_and_synth_logs.md`
