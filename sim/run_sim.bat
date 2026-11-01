@echo off
setlocal
cd /d %~dp0

rem Allow running from Vivado Tcl Shell; normal CMD needs Vivado\bin on PATH
if defined XILINX_VIVADO set "PATH=%XILINX_VIVADO%\bin;%PATH%"
where xvlog >nul 2>nul
if errorlevel 1 (
  echo [ERROR] xvlog not found. Run from Vivado Tcl Shell or add Vivado\bin to PATH.
  exit /b 1
)

if not exist results mkdir results

call xvlog -sv -d SIM_XADC_BEHAVIORAL -log results\xvlog.log ..\src\rtl\afe_top.v ..\src\rtl\tick_gen.v ..\src\rtl\xadc_wrap.v ..\src\rtl\filter_chain.v ..\src\rtl\hp_05hz.v ..\src\rtl\notch_50hz.v ..\src\rtl\fir_lp_40hz.v xadc_wiz_0_sim.v tb_afe_top.v
call xelab -debug typical tb_afe_top -s tb_sim -log results\xelab.log
call xsim tb_sim -runall -log results\xsim.log
if exist output.hex move /y output.hex results\output.hex >nul
echo [OK] simulation finished. Output: sim\results\output.hex
