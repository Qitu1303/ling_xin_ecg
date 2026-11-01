# K2 · 踩坑清单：Vivado Block Design 与 IP 篇（16 条）

> 适用：Vivado IPI（Block Design）流程，任何器件；`[Zynq 通用]` 条目涉及 PS7/HP 口。每条格式：现象 / 触发条件 / 根因 / 排查步骤 / 已验证效果 / 来源。

## K2-1 `set_property` 被静默忽略（门控参数）——`BD 41-721` `[Vivado 通用]`

- **现象**：参数 set 不报错、get 读回新值，但端口/行为始终不出现。
- **触发条件**：给 PS7 之类"复杂 IP"设被门控（disabled）的参数（如 FCLK 使能、中断使能类 `PCW_*` 参数）。
- **根因**：参数处于 disabled 状态，写入被忽略；日志里只有一条非致命 warning：`WARNING: [BD 41-721] Attempt to set value ... on disabled parameter ... is ignored`。
- **排查步骤**：① set 之后**查日志有无 `BD 41-721`**；② "设了但端口不出现"先怀疑门控，读该 IP `component.xml` 的 `<xilinx:isEnabled dependency="...">` 确认依赖链；③ 必要时**换实现路径**，别在门控参数上耗；④ **内存 `report_property` 会撒谎**，唯一可靠判据是 `save_bd_design` 后 grep 磁盘 `.bd`。
- **已验证效果**：某 FCLK 使能参数耗 4 轮往返后换路径（PL 内 clocking wizard 整数分频合成目标频率）10 分钟解决；中断参数补上总闸后端口才出现。
- **来源**：某版 PS7 重配置。

## K2-2 PS7 `IRQ_F2P` 是普通引脚，不是接口引脚 `[Zynq 通用]`

- **现象**：`get_bd_intf_pins` 查中断口全部扑空。
- **触发条件**：按"中断 = 接口"的直觉找 PS7 中断端口。
- **根因**：PS7 的 `IRQ_F2P` 在 BD 里是普通 pin。
- **排查步骤**：用 `get_bd_pins -filter {TYPE =~ "*intr*"}` 找；连完用 `get_bd_pins -of_objects $intc` 列真实脚名复核。
- **已验证效果**：改用 `get_bd_pins` 后立刻找到并连上。
- **来源**：某版中断链接入。

## K2-3 PS7 中断使能是三层链，缺一层端口就不出现 `[Zynq 通用]`

- **现象**：只设 `PCW_IRQ_F2P_INTR=1` 不生效（被 BD 41-721 忽略）。
- **触发条件**：首次给 PS7 挂 PL→PS 中断。
- **根因**：① `PCW_USE_FABRIC_INTERRUPT=1`（总闸）② `PCW_IRQ_F2P_INTR=1`（依赖①）③ validate 后端口才出现。
- **排查步骤**：按 ①②③ 顺序设参 + `save_bd_design` + grep 磁盘 `.bd` + validate；依赖关系不用猜，直接读 PS7 `component.xml` 的 `xilinx:isEnabled`。
- **已验证效果**：补上总闸后 `IRQ_F2P` 端口出现，hwh 中断声明双侧齐全。
- **来源**：同 K2-2。

## K2-4 `axi_intc` v4.1 接入的三个反直觉点 `[Vivado 通用]`

- **现象**：照抄旧 TCL 必踩；留下 `BD 41-1306` + `axi_intc:4.1-13` 两条 WARNING 且 `write_bd_tcl` 复现不了连线。
- **触发条件**：BD 里新增 axi_intc 给 PS7 挂中断；照教程写 `In0`。
- **根因**：① v4.1 **没有 `In0` 标量输入脚**（输入是 `interrupt` 接口 + `intr` 标量并存，pin 与 intf 混连合法）；② **`C_IRQ_CONNECTION` 默认 0=Bus**，连 PS7 标量 `IRQ_F2P` 必须设 **1=Single**；③ `C_NUM_INTR_INPUTS` **read-only、默认即 1**（set 报 `BD 41-737`）。
- **排查步骤**：参数一律先读本机 `data/ip/xilinx/axi_intc_v4_1/component.xml` 的 `spirit:choice` 与 read-only 标记；顺带确认 SmartConnect 的 `NUM_MI` 有没有空闲口（没有先 `set_property CONFIG.NUM_MI {N+1}`）。
- **已验证效果**：按此接入构建成功，hwh 增量恰为 intc 模块与中断链。
- **来源**：某版中断链集成。

## K2-5 失败的 `connect_bd_net` 会留下孤儿网 + 层级穿透脚 `[Vivado 通用]`

- **现象**：后续 validate 报"时钟脚没有有效时钟源"；再次连接又报"net 已存在"的假象。
- **触发条件**：`connect_bd_net A B C…` 里有一个 `get_bd_pins` 返回空 → 命令报错退出但**网已创建**（TCL 命令半成功）。
- **根因**：留下无驱动网与跨层级的穿透脚本。
- **排查步骤**：① 连接前逐项校验 pin 非空；② 清理顺序：`delete_bd_objs [get_bd_nets <孤儿网>]`（引脚自动脱开）→ 删层级穿透脚 → 重新一次性连接；③ 识别法：`get_bd_nets -of_objects [get_bd_pins X]` 看引脚挂在什么怪名下；④ 连好统一重命名网。
- **已验证效果**：按此清理 3 条孤儿网 + 3 个穿透脚后连线干净。
- **来源**：某版视频子系统连线。

## K2-6 硬核 IP 一律放 BD 单元格，不要藏进自定义 IP `[Vivado 通用]`

- **现象**：把含硬核实例的模块打包成自定义 IP 或 Add Module 进 BD，报错难查；两个硬核实例在实现阶段冲突。
- **触发条件**：模块里例化了 XADC/MIG/PCIe 之类硬核，想整体打包。
- **根因**：打包器对含 IP 实例的模块支持差；硬核单实例是器件硬约束。
- **排查步骤**：分工固定为"**硬核 IP → BD 单元格；自己写的纯数字逻辑 → 自定义 IP**"；原 wrapper 拆开：硬核去掉、数字部分进 IP，被搬走的逻辑在新 IP 顶层重写；旧 wrapper 在板上工程 Disable File（只留独立仿真）。
- **已验证效果**：板上路径改为"BD 中的硬核 + 自定义 IP 承载纯逻辑"后自检正常。
- **来源**：某次采集链上板集成。

## K2-7 BD 里新增的 IP 是**独立实例**，不继承工程里同名 IP 的配置 `[Vivado 通用]`

- **现象**：单元格上只有部分端口，缺自己配好的那些——像是"配错了版本"。
- **触发条件**：先生成过一份 IP，再 Add IP 到 BD。
- **根因**：BD 单元格是独立实例，配置需重新下发。
- **排查步骤**：双击单元格 Re-customize，按原配置再配一遍；核对配置读 `Summary` 页或 `.xci`/`.veo`，**别看端口列表**（见 K2-11/K2-12）。
- **已验证效果**：Re-customize 后目标端口全部出现。
- **来源**：同 K2-6。

## K2-8 `.hex`/`.mem` 数据文件放进自定义 IP 会被 packager **静默丢弃** `[Vivado 通用]`

- **现象**：`[IP_Flow 19-5226] Project source file '.../xxx.hex' ignored by IP packager.`；File Groups 里 Verilog 组计数少一。
- **触发条件**：给 `$readmemh` 的系数文件加进 Edit IP 工程。
- **根因**：packager 只收受支持的 HDL 类文件，数据文件被静默丢掉（只是 warning）。
- **排查步骤**：① **数据不进 IP**，两条出路：把数据做进 Verilog（`initial coef[i] = 16'shXXXX;` 或 `localparam`，**推荐**）或 `$readmemh` 传绝对路径（破坏可复现性，不用）；② 生成脚本顺手多输出一个同源 `.v`；③ Packager 里"计数对不上"立刻怀疑有文件被丢弃；④ 换写法后必须跑回归证明"换了写法没换行为"。
- **已验证效果**：几十个系数烘焙进 `.v` 后与原激励金标准逐点比对 **0 LSB 误差 → PASS**。
- **来源**：某次滤波器 IP 打包。

## K2-9 改生成的 AXI 从机模板：读选择器在"用户逻辑区"之前，新信号声明必须上提 `[Vivado 通用]`

- **现象**：仿真器报 `Undefined variable: 'xxx'` + `'xxx' already declared` 连环错。
- **触发条件**：把新信号塞进 `assign S_AXI_RDATA` 三目链，而声明还在下面的用户逻辑区。
- **根因**：Verilog 先声明后用；读选择器位于 `// Add user logic here` **之前**（约文件 3/4 处）。
- **排查步骤**：新信号声明块统一放到读选择器之前并加注释；地址索引提成 `rd_idx` 复用；**改完必须真编译一遍**（这两类问题肉眼看不出）。
- **已验证效果**：声明上提后多模块 0 错 0 警。
- **来源**：两代自定义 IP 各踩一次。

## K2-10 `vlog` 通过 ≠ 连接正确（具名端口连到不存在的端口它不报） `[Vivado 通用]`

- **现象**：端口接错 0 error/0 warning；或缺 Xilinx 原语库时 elaborate 中止。
- **触发条件**：拿"能编译"当正确性证据；核对接第三方 IP 的封装例化。
- **根因**：`vlog` 只做语法级检查，端口连接要到 elaborate/vsim/vopt 才校验。
- **排查步骤**：端口一致性用**权威清单**核（生成的 `.veo` 实例化模板 / `.v` 端口列表 + `.xci` 的开关）；真验端口在 **Vivado xsim** 里 elaborate。
- **已验证效果**：在 `.veo` 里 grep 证实某端口客观不存在，删之零成本；此前被"编译通过"误导一轮。
- **来源**：某次第三方 IP 封装核对。

## K2-11 `Show disabled ports` 会把禁用端口一起列出来 → 据对话框端口列表判配置必错 `[Vivado 通用]`

- **现象**：Wizard 对话框左侧列出一大串端口，据此误判"IP 配置错了"。
- **触发条件**：左上角勾着 `Show disabled ports`。
- **根因**：UI 行为——禁用端口也列出。
- **排查步骤**：判断配置**读 `Summary` 页**或 `.xci`/`.veo`；想看真正启用的端口就取消勾选；最终以放置后的引脚为准。
- **已验证效果**：Summary 页明写实际接口选项——是判据选错，不是配置错。
- **来源**：同 K2-6。

## K2-12 BD 上"看不到某个端口" → 它多半被收进**总线接口**里了 `[Vivado 通用]`

- **现象**：单元格只有几个可展开总线 + 若干单根脚，"找不到"某数据端口，误判成"IP 没生成"。
- **触发条件**：核对任何"端口好像不见了"的 IP（AXI/DRP/视频类都常见）。
- **根因**：BD 把它归到 bus interface（如 `s_drp`）里。
- **排查步骤**：① BD 里点总线左边 `+` 展开；② 权威查法：读 `<工程>.gen/sources_1/bd/<bd>/ip/<bd>_<ip>_0/<bd>_<ip>_0.xml`，搜 `<spirit:busInterface>` 下 `<spirit:portMap>` 的 logicalPort ↔ physicalPort 映射。**别用对话框端口列表判断**（见 K2-11）。
- **已验证效果**：展开总线后 6 个成员齐备，按 portMap 连线成功。
- **来源**：同 K2-6。

## K2-13 DRP 类读数据总线：不发读选通，它永远没数据 `[Zynq 通用]`

- **现象**：计数器正常涨，但数据寄存器**恒 0**；行为模型仿真却"一切正常"。
- **触发条件**：只把读数据总线当结果用；`daddr/den/di/dwe` 悬空；用行为模型仿真过（模型直接把数据推到输出，不看 DRP 握手）。
- **根因**：从没发生过 DRP 读操作。
- **排查步骤**：最小正解——地址 = 目标通道状态寄存器；**DEN = 单周期脉冲**（把转换完成脉冲接过去最省事）；`DWE=0`、`DI=0`；数据用读数据总线，**有效信号用 DRDY 而不是 EOC**（手册原文："data ... is valid on the DO bus when DRDY goes high. Thus DRDY should be used to capture the DO bus."）。仿真结论对 DRP 路径无效，必须行为级打桩或上板验证（见 K3-5）。
- **已验证效果**：按此补全握手后拿到数据；"只连数据线"的写法上板必恒 0。
- **来源**：某次采集链联调（手册：UG480 DRP 章节）。

## K2-14 AXI4 ↔ AXI3 协议不兼容（高带宽 IP 直连 PS HP 口） `[Zynq 通用]`

- **现象**：AXI4 主设备直连 PS `S_AXI_HP0`(AXI3) 报协议不兼容。
- **触发条件**：视频链/高带宽 PL→DDR 通路首次搭建。
- **根因**：PL 里多数 IP 是 AXI4，Zynq HP 口实现的是 AXI3（最大 burst 16）。
- **排查步骤**：插 `axi_protocol_converter`（或经 AXI Interconnect 自动转换）；依据 Xilinx AR 759771 / XAPP792 / UG585。
- **已验证效果**：插 converter 后通过。
- **来源**：某版显示链构建。

## K2-15 自建 IP 接口 `FREQ_HZ` 被 packager 标 fixed → BD 大量 mismatch ERROR `[Vivado 通用]`

- **现象**：BD 里接口频率不匹配，报一片 ERROR。
- **触发条件**：自建 VLNV 兼容 IP（照抄官方 IP 的寄存器面）打包后。
- **根因**：packager 把接口的 `FREQ_HZ` 标成 fixed。
- **排查步骤**：重打包**去掉时钟口 `FREQ_HZ`、只留时钟名**，再 `update_ip_catalog + upgrade_ip` 解锁。
- **已验证效果**：解锁后 BD 正常。
- **来源**：某版视频像素通路集成（自建格式转换 IP）。

## K2-16 较新版本 Vivado 的 `.hwh` 不在 `impl_1/` `[Vivado 通用]`

- **现象**：Generate Bitstream 后 `impl_1/` 里只有 `.bit`，找不到 `.hwh`（很多人卡在这）。
- **触发条件**：Vivado 2025.2 及以后；PYNQ 交付。
- **根因**：硬件 handoff 文件改放 BD 的 `hw_handoff` 目录。
- **排查步骤**：从 `<工程>.gen/sources_1/bd/<bd>/hw_handoff/<bd>.hwh` 复制出来、**改成与 bit 同名**再交付（退路链见 T1 模板 Step 6）。
- **已验证效果**：改名后 `Overlay()` 正常解析。
- **来源**：首次上板教学。
