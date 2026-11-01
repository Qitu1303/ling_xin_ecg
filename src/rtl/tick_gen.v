// tick_gen.v : 产生 500 Hz 单周期脉冲，作为 XADC 的外部 CONVST
// 关键约束：tick 与 XADC 的 dclk 同源（均为 clk），分频比必须是整数
`timescale 1ns/1ps

module tick_gen #(
    parameter CLK_FREQ = 50_000_000,   // 时钟频率（默认 50 MHz）
    parameter RATE     = 500            // 采样率 (S/s)
)(
    input  wire clk,
    input  wire rst_n,
    input  wire en,         // 全局使能
    output reg  tick        // 单周期脉冲，500 Hz
);
    localparam CNT_MAX = CLK_FREQ / RATE - 1;   // 50 MHz -> 99_999
    reg [31:0] cnt;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt  <= 32'd0;
            tick <= 1'b0;
        end else if (!en) begin
            cnt  <= 32'd0;
            tick <= 1'b0;              // 保持清零
        end else if (cnt == CNT_MAX) begin
            cnt  <= 32'd0;
            tick <= 1'b1;
        end else begin
            cnt  <= cnt + 32'd1;
            tick <= 1'b0;
        end
    end
endmodule