// afe_top.v : AFE 顶层 —— 500 Hz tick 触发 XADC 事件采样，再经三级滤波链输出
// 关键约束：clk = FCLK_CLK0（50 MHz）；tick 与 XADC dclk 必须同源，否则漏样
`timescale 1ns/1ps

module afe_top (
    input  wire         clk,              // FCLK_CLK0
    input  wire         rst_n,            // 低有效复位
    input  wire         en,               // 全局使能
    input  wire         filt_en,          // 滤波使能
    input  wire         bypass,           // 滤波旁路开关
    input  wire         vauxp1,           // A0 模拟通道正端
    input  wire         vauxn1,           // A0 模拟通道负端
    // ---- 接口输出 ----
    output wire [15:0]  filt,             // 滤波后输出
    output wire         filt_valid,       // 滤波后有效信号
    output wire [15:0]  sample_raw,       // 原始采样码值
    output wire         sample_raw_valid, // 原始采样有效信号
    output wire [31:0]  sample_cnt        // 采样点数自检计数器
);
    wire tick;
    wire [15:0] xd;
    wire xv;

    // 1. 产生 500Hz 定时 Trigger
    tick_gen #(
        .CLK_FREQ(50_000_000), 
        .RATE(500)
    ) u_tick (
        .clk(clk),
        .rst_n(rst_n),
        .en(en),
        .tick(tick)
    );

    // 2. XADC 事件模式采样封装
    xadc_wrap u_xadc (
        .clk(clk),
        .rst_n(rst_n),
        .convst(tick),
        .vauxp1(vauxp1),
        .vauxn1(vauxn1),
        .sample(xd),
        .sample_valid(xv),
        .sample_cnt(sample_cnt)
    );

    // 3. 滤波链
    filter_chain u_chain (
        .clk(clk),
        .rst_n(rst_n),
        .filt_en(filt_en),
        .bypass(bypass),
        .in_valid(xv),
        .in_data(xd),
        .out_data(filt),
        .out_valid(filt_valid)
    );

    assign sample_raw       = xd;
    assign sample_raw_valid = xv;

endmodule