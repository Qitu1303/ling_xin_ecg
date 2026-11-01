// tb_afe_top.v : AFE 顶层 testbench —— 自检 sample_cnt 计数与滤波输出点数
// 运行：iverilog -DSIM_XADC_BEHAVIORAL -o sim.vvp *.v tb_afe_top.v && vvp sim.vvp
//       xsim 用 -d SIM_XADC_BEHAVIORAL；真实 IP 就绪时去掉该 define。
`timescale 1ns/1ps

module tb_afe_top;
    // ---- 仿真规模：NSAMP 个 XADC 样本后结束（与 stimulus.hex 深度一致） ----
    parameter integer NSAMP = 2048;

    reg         clk     = 1'b0;
    reg         rst_n   = 1'b0;
    reg         en      = 1'b1;
    reg         filt_en = 1'b1;
    reg         bypass  = 1'b0;

    wire [15:0] filt;
    wire        filt_valid;
    wire [15:0] sample_raw;
    wire        sample_raw_valid;
    wire [31:0] sample_cnt;

    integer fout;
    integer k;
    integer raw_cnt;
    integer filt_cnt;
    time    timeout_ns;

    // 实例化 DUT
    afe_top dut (
        .clk(clk),
        .rst_n(rst_n),
        .en(en),
        .filt_en(filt_en),
        .bypass(bypass),
        .vauxp1(1'b0), // 仿真中外部引脚悬空（行为模型内部自带激励）
        .vauxn1(1'b0),
        .filt(filt),
        .filt_valid(filt_valid),
        .sample_raw(sample_raw),
        .sample_raw_valid(sample_raw_valid),
        .sample_cnt(sample_cnt)
    );

    // 生成 50 MHz 时钟（周期 20 ns）
    always #10 clk = ~clk;

    // ---- 计数 both 通道 ----
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            raw_cnt  <= 0;
            filt_cnt <= 0;
        end else begin
            if (sample_raw_valid) raw_cnt  <= raw_cnt  + 1;
            if (filt_valid)       filt_cnt <= filt_cnt + 1;
        end
    end

    // ---- 记录滤波输出到 results/output.hex ----
    initial fout = $fopen("results/output.hex", "w");
    always @(posedge clk) begin
        if (filt_valid) begin
            $fwrite(fout, "%04X\n", filt);
        end
    end

    // ---- 超时保护：行为模型缺失时永远没有 sample_raw_valid，避免静默挂死 ----
    initial begin
        timeout_ns = 64'd2_000_000 * (NSAMP + 100);   // 采样周期 2 ms
        #timeout_ns;
        $display("【ERROR】仿真超时：%0d 个采样周期内未收到足够的样本", NSAMP + 100);
        $display("        请确认：(1) 已给 xadc_wiz_0 准备好 IP 或模型；(2) 已定义 SIM_XADC_BEHAVIORAL");
        $finish;
    end

    initial begin
        $display("===== tb_afe_top 启动：目标 %0d 个样本 =====", NSAMP);

        // 系统初始化复位
        repeat (10) @(posedge clk);
        rst_n = 1'b1;

        // 等 NSAMP 个 XADC 样本
        // NOTE: 用顶层端口而非内部层次引用（dut.xv 端口改名即失效）。
        for (k = 0; k < NSAMP; k = k + 1) begin
            @(posedge sample_raw_valid);
        end

        // 等待两级 clk，避免与 sample_cnt 的自增在同一时刻竞争
        repeat (2) @(posedge clk);

        // 检查是否有漏样
        if (sample_cnt !== NSAMP) begin
            $display("【ERROR】计数异常! sample_cnt = %0d (预期 %0d)", sample_cnt, NSAMP);
        end else begin
            $display("【SUCCESS】采样计数自检正确! sample_cnt = %0d", sample_cnt);
        end

        // 检查滤波通道是否真的有输出（数量应比样本数少若干个 FIR 流水延时）
        if (filt_cnt == 0) begin
            $display("【ERROR】滤波通道全程无输出，检查 filt_en / bypass / 系数文件");
        end else if (filt_cnt > NSAMP) begin
            $display("【ERROR】滤波输出点数 %0d 超过输入 %0d，逻辑异常", filt_cnt, NSAMP);
        end else begin
            $display("【SUCCESS】滤波输出 %0d 点（输入 %0d 点，差值 %0d = 流水线延时）",
                     filt_cnt, NSAMP, NSAMP - filt_cnt);
        end

        if (^filt === 1'bx) begin
            $display("【ERROR】滤波输出存在不定态 X");
        end

        $fclose(fout);
        $display("===== 仿真结束，滤波结果已写入 results/output.hex =====");
        $finish;
    end
endmodule
