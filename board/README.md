# board — 上板工程、运行脚本与实测输出

## 位流与硬件描述

| 文件 | 大小 | MD5 |
|------|------|-----|
| `design_1_wrapper.bit` | 4 045 692 B | `c665651584cf3ea4b142c0846ae811bc` |
| `design_1_wrapper.hwh` | 681 547 B | `8517b2ef2565cba9d8ceace2253e5263` |

版本：v6（2026-10-12），Vivado 2025.2，xc7z020clg400-1。bit 与 hwh **必须同名同目录**；PYNQ Overlay 按 hwh 解析 IP 与地址。

## board_pkg — 可直接上传的板端包

`board_pkg/` 是自包含的 Python 包（`ecgb/` + `board_demo.py` + 寄存器表）。上传整包到：

```text
/home/xilinx/jupyter_notebooks/ecgb_pkg/
```

板上 Python 3.10 只装 numpy；包内不依赖 scipy / matplotlib。

## 上板步骤（Jupyter）

```python
# 1) 路径与自检
import sys
sys.path.insert(0, '/home/xilinx/jupyter_notebooks/ecgb_pkg')
from ecgb.checkup import run_checkup
rep = run_checkup('/home/xilinx/jupyter_notebooks/design_1_wrapper.bit',
                  out_dir='/home/xilinx/jupyter_notebooks/ecg_out', seconds=10)
rep.print_table()
print('checkup ok =', rep.ok)

# 2) 主循环（波形 + 心率 + 显示）
%cd /home/xilinx/jupyter_notebooks/ecgb_pkg
!python3 board_demo.py /home/xilinx/jupyter_notebooks/design_1_wrapper.bit . 120
```

自检八关：写读、采样率标定、冻结、样本在动、输出量程、旁路对照、波形存盘、心率。`rep.ok == True` 即八关全 PASS。

显示：浏览器访问 `http://<板卡IP>:8765`；HDMI 由 PYNQ 视频栈自动配置为 1280×720@60 Hz。推屏需 `board_pkg/ecg_display.py` + PIL + dejavu 字体，缺任一项则本次不推屏并打印提示，采集与网页不受影响。

## 实测输出（`records/`）

6 组板上记录，每组 `part01.csv`（列 `n,code,volt,t_s`）+ `summary.json`。三个时间与计数口径各不相同，读表前先看表后的说明。

| 会话 | `fs`（PL 产样率，EOC 口径） | 样本数 `n_written` | 时长 `duration_s` | 实际投递率 `n÷时长` | `t_s` 跨度 | `frames_dropped` |
|------|------|------|------|------|------|------|
| `1/` | 31.166 S/s | 2937 | 120.14 s | **24.45 S/s** | 94.21 s | 0 |
| `2/` | 30.933 S/s | 2974 | 120.13 s | **24.76 S/s** | 96.11 s | 0 |
| `3/` | 31.172 S/s | 7387 | 300.08 s | **24.62 S/s** | 236.94 s | 0 |
| `4/` | 31.168 S/s | 3135 | 129.65 s | **24.18 S/s** | 100.55 s | 0 |
| `7/` | 31.212 S/s | 1864 | 60.45 s | 30.84 S/s | 59.69 s | 0 |
| `8/` | 31.196 S/s | 1873 | 60.48 s | 30.97 S/s | 60.01 s | 0 |

**口径说明：为什么 `fs` 不等于 样本数 ÷ 时长**

- `fs` 是 **PL 产样率**：`ps_afe.measure_rate()` 读 `SAMPLE_CNT` 硬件计数得到的事件率（`ecgb/ps_afe.py:228-249`），口径为 EOC 事件。
- `n_written` 只统计 **PS 实际收到并写盘的样本**（`plugins/recorder.py:53`）。样本寄存器 0x0C 是单样本读清，两次轮询之间到达的样本会被硬件覆盖、PS 侧读不到。60 s 以上的长会话因此投递率落到 24.2~24.8 S/s（比产样率低约 20~23%），60 s 短会话基本吻合（30.8~31.0）。
- `frames_dropped` **只在写盘队列 `queue.Full` 时 +1**（`recorder.py:228`），统计不到上面那一层。「丢帧 0」与「投递率低于产样率」并不矛盾，但也不能读成样本一个没丢。PS 侧漏收量由 `acq_loop.py:374` 的 `dropped`（硬件计数增量 − n_samples）反映，recorder 未采用该字段。
- `duration_s` 是 `start_session` 到 `finalize` 的全墙钟，含最长 3 s 的写线程 join（`recorder.py:155-162`）。`t_s` 是 `idx / fs_session` 的合成时基（`recorder.py:52`），`fs_session` 取的是会话开始时的标定快照、偏高，因此 `t_s` 跨度短于 `duration_s`。
- `skill/scripts/replay_records.py` 校验分片存在性、`n` 单调与片间连续、并由 `t_s` 步长反解 `fs`；它有意不拿 `t_s` 跨度对 `duration_s`（脚本内注明 `t_s` 是合成时基非墙钟），所以跑它得到「失败 0」不代表上表三项口径自洽。

注意：`summary.json` 中的 `started_wall` 是板卡系统时钟，PYNQ 无 RTC，日期会停在镜像默认值（2025-05-04），不是真实采集日期；时基以 `t_s`（`n/fs` 合成时基）为准。可用 `skill/scripts/replay_records.py` 复核连续性与基线。

## 实测判据

| 项 | 判据 |
|----|------|
| 采样率 | `SAMPLE_CNT` 每秒增量 15~32，标定约 31.3 S/s |
| 心率 | 接入模拟器后与源一致；31.25 S/s 演示口径 HR 72 |
| 网页 | 60 fps 刷新，SSE 推送 ≤10 Hz |
| HDMI | 1280×720@60 Hz，显示波形与心率 |
| 记录 | 600 s/段，样本号连续、无重复写 |

## 安全

交付演示与验收一律只用信号源或心电模拟器取 BPM 真值，不得将电极接入人体做测量。开发调试期操作者本人的短时佩戴仅用于打通链路与核对波形形态，不用于任何测量或结论。本作品输出不构成医疗诊断。
