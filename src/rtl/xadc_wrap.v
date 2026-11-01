// xadc_wrap.v : 事件模式采样，输出 {sample, sample_valid} + 样本计数器
// 关键约束：convst 必须来自 tick_gen（500 Hz）；eoc 即 sample_valid，do_out 直接当 16 bit 无符号码值用
`timescale 1ns/1ps

module xadc_wrap (
    input  wire          clk,          // FCLK_CLK0（与 Wizard dclk 同源）
    input  wire          rst_n,
    input  wire          convst,       // 来自 tick_gen，500 Hz
    input  wire          vauxp1,       // A0 引脚 (VAUXP[1])
    input  wire          vauxn1,       // A0 对应负端
    output wire [15:0]   sample,       // 无符号原始码值 (0..65535)
    output wire          sample_valid, // 每次转换结束的单周期脉冲
    output reg  [31:0]   sample_cnt    // 自检计数器
);
    wire [15:0] do_raw;
    wire        eoc;

`ifdef SIM_XADC_BEHAVIORAL
    // IP 未生成时的行为模型（编译加 +define+SIM_XADC_BEHAVIORAL）
    xadc_wiz_0_sim u_xadc (
        .dclk_in        (clk),
        .reset_in       (~rst_n),
        .convst_in      (convst),
        .vauxp1         (vauxp1),
        .vauxn1         (vauxn1),
        .eoc_out        (eoc),
        .do_out         (do_raw)
    );
`else
    // 真实 XADC Wizard IP（需在 Vivado IP Catalog 中生成 xadc_wiz_0）
    xadc_wiz_0 u_xadc (
        .dclk_in        (clk),
        .reset_in       (~rst_n),
        .convst_in      (convst),      // 事件驱动触发
        .vauxp1         (vauxp1),      // A0 (VAUXP[1]) 通道 17
        .vauxn1         (vauxn1),
        .eoc_out        (eoc),
        .do_out         (do_raw),
        .drdy_out       (),
        .channel_out    (),
        //.eob_out        (),
        .alarm_out      (),
        .eos_out        (),
        .busy_out       ()
    );
`endif

    // do_out 直接作为 16 bit 无符号码值：前提是 Wizard 配 Average=16（16 位全有效）。
    // 上板需核对 Xilinx PG091 的「12-bit MSB aligned」口径：若 do_out 低 4 位恒 0，
    // 须改为 {do_raw[15:4], 4'b0000}，否则整链路幅度小 16 倍、减 32768 的偏置失效。
    assign sample       = do_raw;
    assign sample_valid = eoc;

    // 样本计数器：PS 每秒读取，增量不等于 500 即代表漏样
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            sample_cnt <= 32'd0;
        end else if (sample_valid) begin
            sample_cnt <= sample_cnt + 32'd1;
        end
    end
endmodule
