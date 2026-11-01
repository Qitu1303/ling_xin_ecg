# 第三方部件声明

本文登记 `build/ip_repo/` 下再分发部件的归属与许可。每条的版权行与许可文字均为**源文件头的原文摘录**（逐字抄录，未做改写）；文件头没写的，如实标注待确认，不做推断。

---

## 1. `build/ip_repo/rgb2dvi/`

**读到的版权行原文**（`src/DVI_Constants.vhd`、`src/OutputSERDES.vhd`、`src/SyncAsync.vhd`、`src/SyncAsyncReset.vhd`、`src/TMDS_Encoder.vhd`、`src/rgb2dvi.vhd` 六个文件头，逐字一致）：

```
-- (c) 2014 Copyright Digilent Incorporated
-- All Rights Reserved
```

同一文件头的署名与来源行原文：

```
-- Author: Elod Gyorgy
-- Original Project: HDMI input on 7-series Xilinx FPGA
-- Date: 30 October 2014
```

`OutputSERDES.vhd` 的署名行为 `-- Author: Elod Gyorgy, Mihaita Nagy`；`OutputSERDES.vhd` 与 `TMDS_Encoder.vhd` 的项目名行写作 `HDMI output on 7-series Xilinx FPGA`，其余四个写作 `HDMI input ...`。

**许可标识**：BSD 3-clause。文件头原文：

```
-- This program is free software; distributed under the terms of BSD 3-clause 
-- license ("Revised BSD License", "New BSD License", or "Modified BSD License")
```

其后附完整的三条 Redistribution 条件与 ALL WARRANTIES 免责段。

**头内无声明的文件**：`src/ClockGen.vhd` 的文件头是 Vivado 生成的模板壳，Company / Engineer / Design Name / Description 各字段均留空，只有 `-- Create Date: 11/03/2014 06:27:16 PM` 与 `-- Revision 0.01 - File Created`，**文件头未声明许可，来源待确认**（它随本目录的 Digilent IP 包一同分发）。三个 `.xdc` 约束文件与 `xgui/rgb2dvi_v1_4.tcl` 亦无版权或许可头。

**本包内的用途**：将 24 位并行 RGB 编码为 DVI 串行流，驱动 PYNQ-Z2 板载 HDMI 插座输出波形与心率。`component.xml` 的 IP 标识为 `digilentinc.com:rgb2dvi:1.4`，`spirit:description` 原文 `Encodes a parallel 24-bit RGB video input into DVI 1.0 spec video stream.`

**是否含修改**：六个带声明的源文件未见改写痕迹（全文无本地注释、无新增端口）。本项目的改动在约束层：`build/constraints/pblock_video.xdc` 把像素域 4 个 IP 收敛到 BUFR 所在时钟区域，用于修复 v5 的 hold 违例，未触碰 IP 源码。

---

## 2. `build/ip_repo/axi_dynclk/`

**读到的版权行原文**（`src/mmcme2_drp.v` 文件头，摘录该头的版权与署名部分）：

```
//    Company:          Xilinx
//    Engineer:         Jim Tatsukawa, Karl Kurbjun and Carl Ribbing
//						Sam Bobrowicz (Digilent Inc.)
//    Date:             5/30/2013
//    Design Name:      MMCME2 DRP
//    Module Name:      mmcme2_drp.v
//    Version:          1.03
```

```
//                 (c) Copyright 2009-2010 Xilinx, Inc.
//                 All rights reserved.
```

**许可标识**：该文件头给的是 Xilinx 样本代码免责段。该段在文件中的连续原文行（第 25-41 行，逐行照录，保留原有换行位置）：

```text
//    Disclaimer:  XILINX IS PROVIDING THIS DESIGN, CODE, OR
//                 INFORMATION "AS IS" SOLELY FOR USE IN DEVELOPING
//                 PROGRAMS AND SOLUTIONS FOR XILINX DEVICES.  BY
//                 PROVIDING THIS DESIGN, CODE, OR INFORMATION AS
//                 ONE POSSIBLE IMPLEMENTATION OF THIS FEATURE,
//                 APPLICATION OR STANDARD, XILINX IS MAKING NO
//                 REPRESENTATION THAT THIS IMPLEMENTATION IS FREE
//                 FROM ANY CLAIMS OF INFRINGEMENT, AND YOU ARE
//                 RESPONSIBLE FOR OBTAINING ANY RIGHTS YOU MAY
//                 REQUIRE FOR YOUR IMPLEMENTATION.  XILINX
//                 EXPRESSLY DISCLAIMS ANY WARRANTY WHATSOEVER WITH
//                 RESPECT TO THE ADEQUACY OF THE IMPLEMENTATION,
//                 INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OR
//                 REPRESENTATIONS THAT THIS IMPLEMENTATION IS FREE
//                 FROM CLAIMS OF INFRINGEMENT, IMPLIED WARRANTIES
//                 OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
//                 PURPOSE.
```

紧随其后的是上一小节已照录的版权行（同文件第 43-44 行）。**文件头未使用 BSD / MIT 等具名开源许可，也未给出再分发条件**；按原文如实登记为「Xilinx 免责段 + 版权保留」，具体许可待确认。

**头内无声明的文件**：`src/axi_dynclk.vhd` 与 `src/axi_dynclk_S00_AXI.vhd` 完全没有文件头（首行即 `library ieee;`）——**文件头未声明许可，来源待确认**。`xgui/axi_dynclk_v1_0.tcl`、`component.xml` 同样无许可声明；`component.xml` 的 IP 标识为 `digilentinc.com:axi_dynclk:1.0`，`spirit:description` 原文 `Clock source with a software configurable frequency`。

**本包内的用途**：为像素域提供软件可配频率的动态时钟（MMCM DRP 重配置），使 HDMI 输出稳定在 1280×720@60 Hz 所需的像素时钟上。

**是否含修改**：含，且修改记录就写在同一文件头的 Revisions 段原文里：

```
//    Revisions:        1/13/11 Updated ROM[18,41] LOCKED bitmask to 16'HFC00
//                      5/30/13 Adding Fractional support for CLKFBOUT_MULT_F, CLKOUT0_DIVIDE_F
//						2/02/14 (Digilent, Sam Bobrowicz) Modified to use values provided from a top
//								level to output CLK0 with runtime configurable frequency. Also added
//								parameter for controlling what the default output clock is (affecting
//								the automatically generated timing constraints).
```

即上游为 Xilinx 原始件、经 Digilent 改写后进入 `axi_dynclk` IP。本项目侧未见进一步改动。

---

## 3. `build/ip_repo/color_convert/`

**读到的版权行原文**：无。`src/color_convert.v` 文件头没有任何 `(c)` / Copyright / Author / Company 行。

**许可标识**：**文件头未声明许可，来源待确认**。需要同时登记的一点是：头注释的内容指向本项目自写的接口等价件，而非从 Vivado 取出的 Xilinx HLS 生成物。头注释原文（摘录）：

```
// color_convert.v —— 视频流水线兼容 IP（VLNV: xilinx.com:hls:color_convert:1.0）
//
// 用途：M4（HDMI 与心电采集合 bit）中提供 PYNQ 视频驱动要求的 color_convert
// 实例。官方件是 HLS 生成的 IP，板端 Python 驱动
//    pynq.lib.video.pipeline.ColorConverter
// 只做一件事：读写 12 个 3x4 色空间矩阵系数寄存器（0x10 + 8*i, i=0..11）。
// 因此本 IP 提供等价的 AXI-Lite 寄存器组（可读回、可写），视频通路按
// 24bpp 直通 —— 本工程端到端固定 1280x720 / 24bpp（PIXEL_BGR），
// 单色画面下系数不影响可见结果。
```

**本包内的用途**：向 PYNQ 视频流水线提供驱动层级要求的 `color_convert` 实例；本工程端到端固定 1280×720 / 24bpp，视频通路直通，色空间矩阵系数寄存器仅占位、可读回。

**是否含修改**：不适用（未见上游基线可比）。**须登记一处元数据不一致**：`component.xml` 的 `spirit:vendor` 写作 `xilinx.com`、`spirit:library` 写作 `hls`（见本文末「登记说明」），与上述头注释的自写结论不一致。

---

## 4. `build/ip_repo/pixel_pack/`

**读到的版权行原文**：无。`src/pixel_pack.v` 文件头没有版权或署名行。

**许可标识**：**文件头未声明许可，来源待确认**。头注释原文（摘录）：

```
// pixel_pack.v —— 视频流水线兼容 IP（VLNV: xilinx.com:hls:pixel_pack:1.0）
//
// 用途：M4 中提供 PYNQ 视频驱动要求的 pixel_pack 实例（位于 hdmi_in 占位
// 流水线内，供 3.1.1 驱动层级识别）。板端驱动
//    pynq.lib.video.pipeline.PixelPacker
// 只读写「每像素位数」模式寄存器（0x10）。
```

**本包内的用途**：位于 `hdmi_in` 占位流水线内，供 PYNQ 3.1.1 视频驱动层级识别；本工程 24bpp 直通，模式寄存器仅占位、可读回。

**是否含修改**：不适用（未见上游基线可比）。`component.xml` 的 vendor/library 同样写作 `xilinx.com` / `hls`。

---

## 5. `build/ip_repo/pixel_unpack/`

**读到的版权行原文**：无。`src/pixel_unpack.v` 文件头没有版权或署名行。

**许可标识**：**文件头未声明许可，来源待确认**。头注释原文（摘录）：

```
// pixel_unpack.v —— 视频流水线兼容 IP（VLNV: xilinx.com:hls:pixel_unpack:1.0）
//
// 用途：M4 中提供 PYNQ 视频驱动要求的 pixel_unpack 实例（位于 hdmi_out
// 流水线内）。板端驱动 pynq.lib.video.pipeline.PixelPacker 只读写
// 「每像素位数」模式寄存器（0x10）。本工程端到端固定 24bpp，视频通路
// 直通；模式寄存器仅占位、可读回。
// 附加功能（本项目需要）：**重新生成 SOF（tuser）**。
// VDMA 的 M_AXIS_MM2S 不带 tuser，而下游 v_axi4s_vid_out 以 SOF 做帧同步
// （PG044「SOF / s_axis_video_tuser」节）。因此本 IP 用行计数重建帧首标志：
```

**本包内的用途**：位于 `hdmi_out` 流水线，AXIS 直通并重建帧首标志 SOF（`tuser`）——VDMA 的 `M_AXIS_MM2S` 不带 `tuser`，下游 `v_axi4s_vid_out` 需以 SOF 做帧同步，故用行计数 `0..C_ACTIVE_ROWS-1` 循环重建；输入自带 `tuser` 时按「或」合并。文中引用的 PG044 是 Xilinx 的 VDMA 产品指南编号。

**是否含修改**：不适用（未见上游基线可比）；头注释自述的 SOF 生成为本项目提出的功能。`component.xml` 的 vendor/library 同样写作 `xilinx.com` / `hls`。

---

## 6. `build/ip_repo/hls_video_src/`

本目录是上面三个 IP 的**打包前源码投放处**，内容与其打包产物一一对应。

**读到的版权行原文**：无。`color_convert.v`、`pixel_pack.v`、`pixel_unpack.v` 三个文件的头注释与第 3、4、5 节所述逐字节一致（实测三者与 `color_convert/src/`、`pixel_pack/src/`、`pixel_unpack/src/` 下的对应文件 SHA-256 相同，即同一份源码的两处存放，不是两份不同来源的代码）。

**许可标识**：**文件头未声明许可，来源待确认**。本目录另含打包脚本 `pack_hls_ips.tcl`，其头注释原文（摘录）：

```
# pack_hls_ips.tcl —— 打包 M4 用的 3 个视频兼容 IP
#
# 产出（每个目录一个可被 Vivado 直接引用的 IP）：
#   ip_repo/color_convert    xilinx.com:hls:color_convert:1.0
#   ip_repo/pixel_pack       xilinx.com:hls:pixel_pack:1.0
#   ip_repo/pixel_unpack     xilinx.com:hls:pixel_unpack:1.0
```

脚本中决定 VLNV 的连续原文行（第 31-32 行，保留 Tcl 续行反斜杠）：

```text
    ipx::package_project -root_dir $out \
        -vendor xilinx.com -library hls -name $name -version 1.0 \
```

同一脚本第 38-39 行：

```text
    set_property display_name "PYNQ video-compatible $name (M4)" $core
    set_property description "Interface-compatible replacement for PYNQ's HLS $name used by the LingXin M4 overlay." $core
```

即 `xilinx.com:hls:` 这一 VLNV 由打包命令显式指定，`$name` 是该脚本的 Tcl 变量、打包时逐一取三个 IP 名；脚本自身与三份 RTL 均无 Xilinx 版权声明。

**本包内的用途**：保存三个视频兼容 IP 的可编辑源与一键打包脚本；重建位流时实际被引用的是第 3、4、5 节的打包目录。

**是否含修改**：与第 3、4、5 节同源，无独立改动。

---

## 自研 IP：`build/ip_repo/afe_ctrl_1_0/`

本 IP 是本作品的核心 PL 逻辑，**不含第三方代码的再分发**，归属如下：

- 功能：XADC 封装与 500 Hz `CONVST` 发生（`src/tick_gen.v`）、三级滤波链（`src/filter_chain.v` 及 `hp_05hz.v` / `notch_50hz.v` / `fir_lp_40hz.v`）、AXI Slave Lite 寄存器组（`hdl/afe_ctrl_slave_lite_v1_0_AFE_AXI.v`），以及 `drivers/` 下的 BSP 驱动骨架。
- 著作权归 `ling_xin` 项目作者（见 `LICENSE`），按 MIT 协议发布。
- 需如实登记的边界：`hdl/*_v1_0_AFE_AXI.v`、`hdl/afe_ctrl.v`、`example_designs/`、`drivers/` 与 `xgui/` 由 Vivado IP 打包流程生成的模板骨架派生，这些文件的头部本身**不含任何版权或许可声明**（第一个非空行即 `` `timescale ``）。其中的功能代码与注释为本项目所写。
- `component.xml` 的 IP 标识为 `xilinx.com:user:afe_ctrl:1.0`，其中 vendor 取 `xilinx.com`、library 取 `user`，与上面三个视频 IP 同属打包时的命名选择，不表示该逻辑由 Xilinx 提供。

---

## 登记说明

以下三点按读到的原文如实记录，供审阅者自行判断：

1. `rgb2dvi` 与 `axi_dynclk` 的 `component.xml` 把 vendor 写作 `digilentinc.com`，与文件头的 Digilent / Xilinx 版权声明一致。
2. `color_convert`、`pixel_pack`、`pixel_unpack` 的 `component.xml` 把 vendor 写作 `xilinx.com`、library 写作 `hls`，而这三个目录的 RTL 文件头没有任何 Xilinx 版权声明，头注释与打包脚本均自述为「PYNQ 视频驱动要求的兼容件／Interface-compatible replacement」。VLNV 与文件头版权归属存在不一致，本文不把它读作 Xilinx 官方件。
3. 本文所有许可结论只依据包内文件头。文件头未给出许可的条目已逐条写明「文件头未声明许可，来源待确认」，未据目录名、VLNV 或常识反推许可类型。

---

本包按 MIT 协议发布（见 `LICENSE`）；上述第三方部件按其各自许可分发，本包的 MIT 条款不改变、也不覆盖它们的许可。若某项许可信息在其文件头缺失，已在文中标注待确认。
