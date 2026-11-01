// regs_map.vh —— 由 tools/gen_regmap.py 生成，勿手改
// 给 A 侧 Verilog 用：`include 本文件后直接用宏，避免地址口径漂移
`ifndef ECGB_REGS_MAP_VH
`define ECGB_REGS_MAP_VH

`define ECGB_CTRL          8'h00   // 控制寄存器
`define ECGB_SAMPLE_CNT    8'h04   // 上电以来 EOC 计数，只增不复位
`define ECGB_FILT          8'h08   // 滤波输出
`define ECGB_RAW           8'h0C   // 原始样本 + 新数据标志

`define ECGB_CTRL_EN       1'b1        // [0] 全局使能
`define ECGB_CTRL_FILT_EN  1'b1 << 1   // [1] 滤波使能
`define ECGB_CTRL_BYPASS   1'b1 << 2   // [2] 旁路
`define ECGB_BAND_LSB      2           // [3:2] 带宽档位（预留未实现）
`define ECGB_BAND_MASK    2'b11 << `ECGB_BAND_LSB

`endif
