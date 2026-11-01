# P3 · 提示词工作流：Vivado TCL 程序化出 bit（不开 GUI 的完整闭环）

- **类型**：提示词工作流
- **适用场景**：需要程序化（Agent 驱动 / CI / 脚本）完成 Block Design 改造、IP 集成、约束修复并出 bit；或"上板同错复现"时需要可重放、可 diff 的构建过程。
- **使用方法**：按四阶段顺序执行；每一步的 TCL **落盘成脚本文件**再 source（不要在对话/命令行里逐条拼——JSON 参数套 TCL 引号极易出错），失败可 diff、成功可重跑。
- **失效条件**：① 依赖 Vivado batch/TCL 能力（`vivado -mode batch -source` 或等价 MCP 工具），且会话必须常驻后台（实测随父终端死、端口静默漂移是两大坑）；② 仅适用 IPI（Block Design）流程，纯 RTL 工程只适用阶段 2~3；③ `综合绿 ≠ 能出 bit`——BIVC 类 DRC 在 place 爆、PDRC 类在 bitgen 爆，必须跑到 write_bitstream 完成才算过。
- **已验证效果**：某版构建一次全流程走通（改 PS 配置 + 新增 15 个 IP 的视频子系统 + 三轮 DRC 修复 + 两轮出 bit+hwh，全程零 GUI）；7z020 全流程（含 PS OOC 重综合）约 25~30 分钟。
- **来源**：某版 BD 改造：FCLK1 门控参数在 GUI 思维下耗 4 轮往返，换"落盘脚本 + 磁盘核对"纪律后同类问题 10 分钟解决；自此固化为四阶段。

---

## 提示词模板

```text
你是 Vivado IPI 自动化构建工程师。请按四阶段完成 <任务描述>，全程 TCL 落盘执行、零 GUI。

【阶段 0：起会话 + 备份】
1. 启动 batch 会话（session 常驻后台），记录 session 标识，后续所有调用必带；
2. 动手前备份 <工程>.bd / <工程>.xpr / <约束>.xdc 到备份目录，报告各文件字节数核对；
3. 所有 BD 改造写成 build_*.tcl 落盘（create_bd_cell / connect_bd_net / set_property …），
   一次 source 执行；单条 TCL 失败时修正脚本后整脚本重跑，不做半程补丁。

【阶段 1：分批推进 + 每批 validate】
按批推进：PS 重配 → 加 IP/层级 → 连线 → 地址分配 → 外部端口。
每批结束 validate_bd_design；CRITICAL/WARNING 逐条对照，已知可接受的记入白名单：
  白名单：<如 BD 41-49 抽象接口定义缺失、BD 41-759 占位 IP tie-off>
白名单之外的任何新码出现即停下处理。

【阶段 2：构建（异步）】
reset_run synth_1; launch_runs impl_1 -to_step write_bitstream -jobs <N>   # 立即返回，属正常
轮询 run 状态与日志尾，终态判据 = write_bitstream Complete! 100%；
出错读 <runs>/impl_1/runme.log 搜 ERROR/CRITICAL 定位。
注意：BIVC 类（VCCO 冲突）在 place 爆、PDRC 类（VCO 越界）在 bitgen 才爆——
      "综合通过"不是放行条件。

【阶段 3：出包与核对（缺一不可）】
1. 磁盘 .bd 核对：save_bd_design 后 grep 磁盘 .bd 文件确认关键 set_property 真的生效
   （内存里的 report_property 会撒谎，门控参数被静默忽略时只有日志 BD 41-721 一条 warning）；
2. hwh grep 三件事：关键 IP 的 <MEMRANGE BASEVALUE=...> 基址、<MODULE INSTANCE=...> 实例清单、
   驱动需要的中断声明（SIGIS="INTERRUPT"）双侧齐全；
3. 字节对账：报告 bit/hwh 的字节数与 MD5——bit 只按 MD5 定版本
   （实测相邻六版 bit 字节数完全相同、MD5 全不同；hwh 字节数才随 BD 结构变化）；
4. 交付目录只增不覆盖：<OUT_DIR>/v<N>/ 放 bit + 同名 hwh + 时序报告 + README
   （README 必备：日期/改动/理由/时序前后对比/文件字节数/回滚路径/上板验证判据）。

【输出要求】
每阶段末尾给"本阶段产物清单 + 判据核对结果"；任何白名单条目注明"为何可接受"。
```

## 阶段要点与 Verify 判据（Step 1..N）

| Step | 动作 | Verify |
|---|---|---|
| 1 | 会话常驻 + 备份三件 | 备份文件字节数与原件一致；session 心跳可用 |
| 2 | TCL 落盘分批执行 | 每批 validate 后新增 CRITICAL = 白名单内 |
| 3 | 磁盘 .bd 核对 | `grep <参数名> <工程>.bd` 与期望值一致 |
| 4 | 构建轮询 | `write_bitstream Complete! 100%`；runme.log 无新 ERROR |
| 5 | hwh grep | 基址/实例/中断声明三组模式各命中预期数量 |
| 6 | 哈希定版 | bit MD5 与台账比对；hwh 变化与 BD 改动对应 |
| 7 | 版本目录 | v<N> 只增不覆盖；README 六要素齐全 |

## 配套坑位速查（构建期高频）

1. **PS7 门控参数静默忽略**（`WARNING: [BD 41-721] Attempt to set value ... on disabled parameter ... is ignored`）：set 不报错、get 读回新值、但端口/行为不出现。两步判别：查日志 `BD 41-721`；读该 IP `component.xml` 的 `xilinx:isEnabled dependency` 确认参数依赖。处理：补上依赖的总闸参数，或**换实现路径**（实测案例：PS FCLK1 拉不出来 → 改 PL 内 clocking wizard 从 FCLK0 整数倍合成目标频率，全整数分频一次到位）。
2. **失败的 `connect_bd_net` 留孤儿网**：命令里任一 `get_bd_pins` 返回空 → 命令报错退出但网已创建；下次连接报"net 已存在"。清理顺序：`delete_bd_objs [get_bd_nets <孤儿>]` → 删层级穿透脚 → 重新一次性连接；连接前逐项校验 pin 非空。
3. **`axi_intc` v4.1 三反直觉**：无 `In0` 标量输入脚（接口+标量混连合法）；`C_IRQ_CONNECTION` 连 PS 标量中断必须设 `Single`；`C_NUM_INTR_INPUTS` read-only（set 报 `BD 41-737`）。参数先读本机 IP `component.xml`。
4. **版本目录纪律**：板上实际加载路径可能与交付目录名完全无关（文件名不含版本号）——**目录名 ≠ 板上版本**，以 MD5 台账为准。

## 预期输出与判读

- 阶段 0~1：备份清单 + 白名单表（码 / 出处 / 可接受理由）；
- 阶段 2：run 终态日志行 + runme.log ERROR 计数 = 0；
- 阶段 3：`check_bit_pair.py` 全 `[OK]`；`check_timing.py` 判读结果（pass 或明确违例清单）；版本目录新增 v<N>。

## 来源

某版 BD 改造（视频子系统 15 IP + 中断链接入）：GUI 思维下 FCLK1 门控参数耗 4 轮往返未果；转为"build TCL 落盘 + 磁盘 .bd grep"后改用 clocking wizard 合成路径 10 分钟解决。同版构建实测六个版本 bit 字节数完全相同（约 3.9 MB），据此把"哈希定版"固化为阶段 3 硬条款；配套脚本见 `../scripts/check_bit_pair.py` 与 `../scripts/check_timing.py`。
