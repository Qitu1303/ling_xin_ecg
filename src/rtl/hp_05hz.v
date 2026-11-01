// hp_05hz.v : 0.5 Hz 一阶高通（y[n] = ALPHA*(y[n-1] + x[n] - x[n-1])，Q15 定点）
`timescale 1ns/1ps

module hp_05hz #(
    parameter IW = 18,
    parameter ALPHA_Q15 = 16'sd32564    //决定一阶高通的截止频率
)(
    input  wire                   clk,
    input  wire                   rst_n,
    input  wire                   in_valid,
    input  wire signed [IW-1:0]   in_data,
    output reg  signed [IW-1:0]   out_data,
    output reg                    out_valid
);
    reg signed [IW-1:0] x_d, y_d;

    wire signed [IW+1:0] xsum = {y_d[IW-1], y_d[IW-1], y_d}
                          + {in_data[IW-1], in_data[IW-1], in_data}
                          - {x_d[IW-1], x_d[IW-1], x_d};

    wire signed [47:0]   prod = ALPHA_Q15 * xsum;

    wire signed [IW-1:0] ynext = prod >>> 15;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            x_d       <= {IW{1'b0}};
            y_d       <= {IW{1'b0}};
            out_valid <= 1'b0;
            out_data  <= {IW{1'b0}};
        end else begin
            out_valid <= 1'b0;
            if (in_valid) begin
                out_data  <= prod >>> 15;
                out_valid <= 1'b1;
                x_d       <= in_data;
                y_d       <= prod >>> 15;
            end
        end
    end
endmodule