# T1 · 案例模板：上板最小演示工程（自定义 AXI IP → BD → bit → PYNQ Python）

- **类型**：案例模板（工程骨架）
- **适用场景**：新题目/新板卡首次把"自己写的 Verilog"送上 PYNQ 板并用 Python 控制的最短闭环；验证工具链通路的冒烟工程。
- **使用方法**：按下面 7 步顺序走，每步末尾有"过关判据"；模板外设以"4 位 LED 流水灯 + 1 个可读写寄存器"为例，替换成你的 RTL 时只换【用户逻辑】部分。
- **失效条件**：① 非 AXI-Lite 外设（如需要中断/DMA 的外设）要追加对应集成步骤（中断链参考 K2 篇 axi_intc 条目）；② 非 Zynq-7000 器件需替换器件选型与时钟来源；③ 本模板假设 PYNQ 镜像已能启动（起不来先查 K1 篇启动三段判据）。
- **已验证效果**：该骨架在 PYNQ-Z2（xc7z020clg400-1）+ Vivado 2025.2 + PYNQ 3.1.1 实板走通：时序满足、DRC 0 违规、Python 写读寄存器与板上灯光一致；后续更复杂工程（传感器采集链）沿用同一骨架只扩用户逻辑。
- **来源**：首次上板教学中实测踩过的 10 个坑（文末坑表），每条都对应一次真实返工。

---

## 骨架总览

```
[你的 RTL 纯逻辑] ──包进──> [自定义 AXI4-Lite IP] ──挂到──> [BD: Zynq PS7 + SmartConnect + 你的IP]
                                                                 │ XDC 引脚约束
                                                                 ▼
                                              [synth→impl→bit] + [hw_handoff 的 .hwh 改名同名]
                                                                 │ scp/Jupyter 上传
                                                                 ▼
                                       板上 Python: Overlay(bit) → ol.<ip名>.write/read
```

## Step 1 建工程：Parts 手选器件

新工程 → Parts 页搜 `xc7z020clg400-1`（PYNQ-Z2；别选成列表里扎堆的 Kintex `xc7k70t…`）。若本机未装 TUL 板卡文件，Boards 页签搜不到属正常，Parts 手选即可；装了板卡文件也别假设每台机器都有。
**判据：** 工程器件属性 = 目标型号；`[Board 49-26]` 刷屏类告警无害，出得了 bit 就忽略。

## Step 2 生成 AXI IP 骨架并改三处

Tools → Create and Package New IP → Create AXI4 Peripheral（Slave only, Lite）。

1. **顶层**（`<ip>.v`，旧版向导为 `<ip>_v1_0.v`）：加用户端口（如 `output wire [3:0] led_o`），并在**从机实例化端口列表**末尾补 `.led_o(led_o)`——实例化在**顶层**文件；`<ip>_S00_AXI.v` 是叶子模块，本来就没有实例化，别在它里面找。
2. **从机**（`<ip>_slave_lite_v1_0_S00_AXI.v`）：在 `// Add user logic here` 区插逻辑（时钟 `S_AXI_ACLK`、复位低有效 `S_AXI_ARESETN`）；寄存器写在 `slv_reg0..3`（已在 `always` 块里，**别再开第二个 always 写它们** → multi-driven net）。
3. **读选择器**：`assign S_AXI_RDATA` 的三目链位于用户逻辑区**之前**（约文件 3/4 处）——新信号**声明必须上提**到它之前，否则 `Undefined variable` / `already declared` 连环报错。顺手把地址索引提成 `wire [1:0] rd_idx = axi_araddr[ADDR_LSB+OPT_MEM_ADDR_BITS:ADDR_LSB];` 复用。

**判据：** ModelSim/Vivado 编译 0 error 0 warning；只读寄存器在读选择器里返回、`slv_reg` 留空。

## Step 3 Re-Package IP（改了 HDL 必做）

BD 里看到的端口由 **`component.xml`** 决定。Edit IP 临时工程 → Packager 左侧 `File Groups` → `Merge changes from edit_<ip>` → `Ports and Interfaces` 确认新端口在列 → `Review and Package` → **Re-Package IP** → 回主工程 Refresh IP Catalog / Upgrade IP。
**判据（不靠肉眼）：** `(Select-String component.xml -Pattern '<spirit:name>\S*\.(v|hex)</spirit:name>').Count` 与实际文件数一致。文件被放进 `src/` 而非 `hdl/` 属正常，**别手动搬**（会破坏 component.xml 相对路径）。

## Step 4 建 BD：PS7 + SmartConnect + 你的 IP

Create Block Design → Add IP：Zynq7 Processing System（Run Block Automation 带 DDR/FIXED IO）+ AXI SmartConnect + 你的 IP；Run Connection Automation 接 S_AXI。地址分配后 `assign_bd_address`。
**判据：** `validate_bd_design` 无新 ERROR；Address Editor 里你的 IP 段位宽 = 4K（Minimal）。

## Step 5 引出端口 + XDC（按实际名写）

BD 里右键端口 → Make External。**外部端口名以 BD 实际生成为准**：实测 `led[3:0]` 导出后叫 `led_o_0`（不是 `led_o_0[3:0]` 想当然的写法）；模拟差分口 `diff_analog_io` 展开为 `Vaux1_0_v_p/n`。XDC 引脚号查你板卡的官方 XDC/原理图，**方法见 K3 篇"四环溯源链"**。
**判据：** `get_ports` 需先 Open Elaborated Design（否则 `[Common 17-53] No open design`）；或直接读 `<工程>.gen/sources_1/bd/<bd>/hdl/<bd>_wrapper.v` 的端口列表；`get_ports {led_o_0[0]}` 大括号不能省。XDC 里**别写中文注释**（个别版本编码报错）。

## Step 6 综合→实现→bit，取 hwh 改同名

Generate Bitstream。**hwh 不在 `impl_1/`**（较新版本 Vivado 行为）：到 `<工程>.gen/sources_1/bd/<bd>/hw_handoff/<bd>.hwh` 复制出来，**改成与 bit 同名**，与 bit 同目录交付。
**判据：** 时序报告无违例（`../scripts/check_timing.py` 判读）；`../scripts/check_bit_pair.py` 对交付目录全 `[OK]`。退路链：同名 `.hwh` → 同名 `.xsa`（Export Hardware 勾 Include bitstream）→ `MMIO(基址, 长度)` 硬编码（基址抄 hwh 的 `BASEVALUE`）。

## Step 7 板上 Python 最小链路

```python
from pynq import Overlay
ol = Overlay("/home/<user>/<dir>/<top>_wrapper.bit")   # bit+hwh 同名同目录
print(list(ol.ip_dict))            # 看真实实例名
ip  = ol.<你的ip实例名>             # ★ 属性访问（PYNQ 3.x：ip_dict 条目没有 base_address 键）
ip.write(0x00, 0x1)                # offset 是相对 IP 基址的字节偏移，与 slv_reg0..3 一一对应
print(hex(ip.read(0x04)))
```

**判据（判据链，缺一关别往下走）：** ① 写读自检（写 0x00 回读 = 0x1）② 若有速率类行为，测速率（2~3 s 窗口，短窗会被量化误差骗）③ 使能关断（`en=0` 后行为停止——证明真在受控）④ 输出在动（不同码值/翻转计数）⑤ 功能项。注意 `clock_dict[0]` 是普通 dict（`.frequency` 会 AttributeError）；`Overlay()` 默认重配 PL，新建对象后控制寄存器回默认值，**使能要重新写**。

## 坑表（10 条实测）

| # | 坑 | 正解 |
|---|---|---|
| 1 | 器件选成 Kintex `xc7k70t…` | Parts 搜 `xc7z020clg400-1` |
| 2 | `FCLK_CLK0` 默认 50 MHz，逻辑按别的频率写 | 按板上实际频率设计（先 `print(ol.clock_dict)` 验证）；`rst_*_50M` 这类名字只是名字，不影响功能 |
| 3 | 改 HDL 后 BD 看不到新端口 | 回打包器 `Re-Package IP`（判据：grep component.xml） |
| 4 | 在 `_S00_AXI.v` 里找实例化 | 实例化在**顶层**文件 |
| 5 | 以为 hwh 在 `impl_1/` | 在 `.gen\sources_1\bd\<bd>\hw_handoff\`，复制后**改名与 bit 同名** |
| 6 | 外部端口名与 XDC 对不上 | 以 BD 实际名写（`led_o_0` / `Vaux1_0_v_p`），大小写敏感 |
| 7 | 综合报 XDC 编码错误 | XDC 别写中文注释 |
| 8 | `[Board 49-26]` / `Spawn failed` 刷屏 | 板卡文件不匹配的告警，出得了 bit 就忽略 |
| 9 | 读选择器引用新信号报 `Undefined variable` | 声明上提到读选择器之前 |
| 10 | 同一模块在 IP 与 Design Sources 各一份 | 模块重复定义；只留 IP（IP 自包含），工程侧 Disable File |

## 来源

某队首次把自写 Verilog 送上 PYNQ-Z2 的教学工程（LED 流水灯 + 寄存器控制）全程实测；10 条坑即当时返工实录。后续传感器采集工程（模拟输入 → Zynq XADC → 滤波链 IP）复用该骨架，仅用户逻辑与 XDC 不同。
