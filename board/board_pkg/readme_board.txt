board_pkg —— 上传到 PYNQ-Z2 的整包（自包含 ecgb/ 包，相对 import 原样可用）

目标：整个 board_pkg 目录传到 /home/xilinx/jupyter_notebooks/ecgb_pkg/

用法（Jupyter）：
  import sys; sys.path.insert(0, '/home/xilinx/jupyter_notebooks/ecgb_pkg')
  from ecgb.checkup import run_checkup
  rep = run_checkup('/home/xilinx/jupyter_notebooks/design_1_wrapper.bit',
                    out_dir='/home/xilinx/jupyter_notebooks/ecg_out', seconds=10)
  rep.ok

或直接跑演示：
  cd /home/xilinx/jupyter_notebooks/ecgb_pkg && python3 board_demo.py /home/xilinx/jupyter_notebooks/design_1_wrapper.bit . 120

0x00 位定义（与团队《三人数据流接口表》Sheet④ 一致）：
  [0] en  [1] filt_en  [2] bypass  [3:2] 带宽档位（预留未实现）  [5..31] 预留
  常用写值：0x1 直通原始 / 0x3 滤波 / 0x5 强制旁路 / 0x0 停采样

注意：
  - 板上 Python 3.10 没有 scipy/matplotlib；本包的采集与心率主链只用 numpy + 标准库。HDMI 推屏例外：随包的 ecg_display.py 还需 PIL 与 dejavu 字体，缺 PIL 时打印「本次不推屏」并退化为 NullDisplay，采集与网页不受影响；
  - .bit 与 .hwh 必须同名同目录（Vivado 2025.2 的 .hwh 在 <工程>.gen/.../hw_handoff/ 下）；
  - 安全红线：交付演示与验收只用信号源/心电模拟器取真值，不得接入人体做测量（开发期操作者本人的链路打通除外，不构成测量结论）。
