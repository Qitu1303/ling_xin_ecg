// notch_50hz.v : 50 Hz 梳状陷波（fs = 500 Hz）
`timescale 1ns/1ps

// y[n] = (x[n] + x[n-DLY])/2，零点在 f = fs*(2k+1)/(2*DLY)；
// DLY = fs/(2*50) = 5 是半周期延迟，50 Hz 两样本反相相消。整周期 DLY=10 反而在 50 Hz 形成增益峰。
module notch_50hz #(
    parameter IW  = 18,
    parameter DLY = 5                 // fs=500 -> 陷波点 50 / 150 / 250 Hz
)(
    input  wire                  clk,
    input  wire                  rst_n,
    input  wire                  in_valid,
    input  wire signed [IW-1:0]  in_data,
    output reg  signed [IW-1:0]  out_data,
    output reg                   out_valid
);
    reg signed [IW-1:0] dline [0:DLY-1];   //delay line 
    integer i;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (i = 0; i < DLY; i = i + 1) begin
                dline[i] <= {IW{1'b0}};
            end
            out_valid <= 1'b0;
            out_data  <= {IW{1'b0}};
        end else begin
            out_valid <= 1'b0;
            if (in_valid) begin
               out_data <= ($signed({in_data[IW-1], in_data})
                        + $signed({dline[DLY-1][IW-1], dline[DLY-1]})) >>> 1;     // >>> 是算术移位，右边补上的是符号位
                out_valid <= 1'b1;
                for (i = DLY-1; i > 0; i = i - 1) begin
                    dline[i] <= dline[i-1];
                end
                dline[0] <= in_data;
            end
        end
    end
endmodule
