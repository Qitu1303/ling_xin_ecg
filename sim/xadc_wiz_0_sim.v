// xadc_wiz_0_sim.v : XADC Wizard 行为模型，仅 +define+SIM_XADC_BEHAVIORAL 时被 xadc_wrap 引用
// 激励优先读 ../data/stimulus.hex（与 golden.hex 同源），文件缺失时退回内置解析式波形。
// 输出 16 bit 无符号码值，中值 32768（等价 Wizard Average=16 口径）。
`timescale 1ns/1ps

module xadc_wiz_0_sim #(
    parameter integer CONV_LATENCY = 40,        // 转换完成后到 EOC 的 dclk 拍数
    parameter real    FS           = 500.0,     // 采样率 (S/s)
    parameter integer STIM_N       = 2048,      // stimulus.hex 深度
    parameter         STIM_FILE    = "../data/stimulus.hex"
)(
    input  wire        dclk_in,
    input  wire        reset_in,
    input  wire        convst_in,
    input  wire        vauxp1,
    input  wire        vauxn1,
    output reg         eoc_out,
    output reg  [15:0] do_out
);
    localparam real PI = 3.141592653589793;

    integer n                = 0;      // 已完成的转换序号
    reg [15:0] code16        = 16'd0;  // 当前待回送的码值
    reg [15:0] stim [0:STIM_N-1];      // 外部激励表
    reg        use_file      = 1'b0;
    reg [7:0]  wait_cnt      = 8'd0;
    reg        conv_busy     = 1'b0;
    reg        convst_d      = 1'b0;
    integer    fd;

    wire convst_rise = convst_in & ~convst_d;

    // ---- 内置解析式激励：16-bit 有符号量，中值 32768 ----
    function automatic [15:0] sample_val;
        input integer k;
        real t, v;
        begin
            t = k / FS;
            v = 32768.0
              + 10000.0 * $sin(2.0 * PI * 0.8  * t)   // 有用信号 0.8 Hz
              +  2000.0 * $sin(2.0 * PI * 0.15 * t)   // 基线漂移 0.15 Hz
              +  8000.0 * $sin(2.0 * PI * 50.0 * t);  // 50 Hz 工频干扰
            if (v < 0.0)     v = 0.0;
            if (v > 65535.0) v = 65535.0;
            sample_val = $rtoi(v);
        end
    endfunction

    initial begin
        fd = $fopen(STIM_FILE, "r");
        if (fd == 0) begin
            use_file = 1'b0;
            $display("[xadc_wiz_0_sim] 未找到 %0s，使用内置解析式激励", STIM_FILE);
        end else begin
            $fclose(fd);
            $readmemh(STIM_FILE, stim);
            use_file = 1'b1;
            $display("[xadc_wiz_0_sim] 已从 %0s 载入 %0d 个样本", STIM_FILE, STIM_N);
        end
    end

    always @(posedge dclk_in or posedge reset_in) begin
        if (reset_in) begin
            convst_d <= 1'b0;
        end else begin
            convst_d <= convst_in;
        end
    end

    always @(posedge dclk_in or posedge reset_in) begin
        if (reset_in) begin
            n         <= 0;
            wait_cnt  <= 8'd0;
            conv_busy <= 1'b0;
            eoc_out   <= 1'b0;
            do_out    <= 16'd0;
            code16    <= 16'd0;
        end else begin
            eoc_out <= 1'b0;
            if (convst_rise && !conv_busy) begin
                // 事件触发：锁存当前采样值，延时若干拍后给出 EOC
                code16    <= use_file ? stim[n % STIM_N] : sample_val(n);
                n         <= n + 1;
                wait_cnt  <= CONV_LATENCY[7:0];
                conv_busy <= 1'b1;
            end else if (conv_busy) begin
                if (wait_cnt == 8'd1) begin
                    do_out    <= code16;
                    eoc_out   <= 1'b1;
                    conv_busy <= 1'b0;
                    wait_cnt  <= 8'd0;
                end else begin
                    wait_cnt <= wait_cnt - 8'd1;
                end
            end
        end
    end

endmodule
