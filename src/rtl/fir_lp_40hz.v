// fir_lp_40hz.v : 63 阶线性相位低通，fs = 500 Hz，fc = 40 Hz，Hamming 窗，Q1.15 系数
// 系数直接烘焙在下方 initial 块：Vivado IP packager 会丢弃 .hex 数据文件，
// 一旦 $readmemh 找不到文件则系数全 0。换系数用 gen_fir_coef.py 重新生成并替换。
`timescale 1ns/1ps

module fir_lp_40hz #(
    parameter IW        = 18,
    parameter TAPS      = 63,
    parameter COEF_FILE = "fir_coef.hex"   // 已弃用：系数已写死在下方 initial 块，本参数不再使用
)(
    input  wire                   clk,
    input  wire                   rst_n,
    input  wire                   in_valid,
    input  wire signed [IW-1:0]   in_data,
    output reg  signed [IW-1:0]   out_data,
    output reg                    out_valid
);
    (* rom_style = "block" *) reg signed [15:0] coef [0:TAPS-1]; // Q1.15
    reg signed [IW-1:0] hist [0:TAPS-2];  // 历史样本 (TAPS-1 = 62 级)
    reg signed [IW-1:0] xin;
    reg signed [47:0]   acc;
    reg [7:0]           idx;
    reg                 busy;
    integer i;

    // ---- 系数直接赋值（Q1.15 16 bit 有符号，值 = fir_coef.hex 的内容，按顺序）----
    initial begin
		coef[ 0] = 16'sh0003; coef[ 1] = 16'sh0011; coef[ 2] = 16'sh001D; coef[ 3] = 16'sh0025;
		coef[ 4] = 16'sh0026; coef[ 5] = 16'sh001B; coef[ 6] = 16'sh0000; coef[ 7] = 16'shFFD8;
		coef[ 8] = 16'shFFAB; coef[ 9] = 16'shFF86; coef[10] = 16'shFF7B; coef[11] = 16'shFF99;
		coef[12] = 16'shFFE6; coef[13] = 16'sh005A; coef[14] = 16'sh00DE; coef[15] = 16'sh014A;
		coef[16] = 16'sh0174; coef[17] = 16'sh0136; coef[18] = 16'sh0082; coef[19] = 16'shFF69;
		coef[20] = 16'shFE1F; coef[21] = 16'shFCF9; coef[22] = 16'shFC5B; coef[23] = 16'shFCA5;
		coef[24] = 16'shFE1A; coef[25] = 16'sh00C7; coef[26] = 16'sh0480; coef[27] = 16'sh08DA;
		coef[28] = 16'sh0D3C; coef[29] = 16'sh10FF; coef[30] = 16'sh1389; coef[31] = 16'sh1470;
		coef[32] = 16'sh1389; coef[33] = 16'sh10FF; coef[34] = 16'sh0D3C; coef[35] = 16'sh08DA;
		coef[36] = 16'sh0480; coef[37] = 16'sh00C7; coef[38] = 16'shFE1A; coef[39] = 16'shFCA5;
		coef[40] = 16'shFC5B; coef[41] = 16'shFCF9; coef[42] = 16'shFE1F; coef[43] = 16'shFF69;
		coef[44] = 16'sh0082; coef[45] = 16'sh0136; coef[46] = 16'sh0174; coef[47] = 16'sh014A;
		coef[48] = 16'sh00DE; coef[49] = 16'sh005A; coef[50] = 16'shFFE6; coef[51] = 16'shFF99;
		coef[52] = 16'shFF7B; coef[53] = 16'shFF86; coef[54] = 16'shFFAB; coef[55] = 16'shFFD8;
		coef[56] = 16'sh0000; coef[57] = 16'sh001B; coef[58] = 16'sh0026; coef[59] = 16'sh0025;
		coef[60] = 16'sh001D; coef[61] = 16'sh0011; coef[62] = 16'sh0003;
    end

    // idx == 0 时取当前输入，其余取 hist[idx-1]。
    // 必须先钳位再索引：idx == 0 时 hist[idx-1] 会越界到 hist[255]（仿真为 X）。
    wire [7:0]          idx_m1 = (idx == 8'd0) ? 8'd0 : (idx - 8'd1);
    wire signed [IW-1:0] xsel  = (idx == 8'd0) ? xin : hist[idx_m1];
    wire signed [47:0]   last  = acc + coef[TAPS-1] * xsel;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            busy      <= 1'b0;
            out_valid <= 1'b0;
            idx       <= 8'd0;
            acc       <= 48'd0;
            xin       <= {IW{1'b0}};
            out_data  <= {IW{1'b0}};
            for (i = 0; i < TAPS-1; i = i + 1) begin
                hist[i] <= {IW{1'b0}};
            end
        end else begin
            out_valid <= 1'b0;
            if (!busy) begin
                // 空闲：捕获新样本，启动一轮 TAPS（抽头数） 次乘累加
                if (in_valid) begin
                    xin  <= in_data;
                    acc  <= 48'd0;
                    idx  <= 8'd0;
                    busy <= 1'b1;
                end
            end else begin
                if (idx == TAPS-1) begin
                    // 最后一拍：连同最后一个抽头一起累加后输出（Q1.15 还原）
                    out_data  <= last >>> 15;
                    out_valid <= 1'b1;
                    busy      <= 1'b0;
                    for (i = TAPS-2; i > 0; i = i - 1) begin
                        hist[i] <= hist[i-1];
                    end
                    hist[0] <= xin;
                end else begin
                    acc <= acc + coef[idx] * xsel;
                    idx <= idx + 8'd1;
                end
            end
        end
    end
endmodule
