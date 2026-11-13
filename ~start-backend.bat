@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "HOST=127.0.0.1"
rem 默认使用 6278，避开 Windows/Hyper-V 可能保留的端口段，防止绑定时报 10013。
set "PORT=6278"
set "LOG=.zcaisagent\backend.log"

rem 选一个装好 uvicorn + click 的 Python（避免命中残缺的解释器）
rem 顺序：项目虚拟环境 -> PATH 中的 python -> 当前激活的 conda 环境 -> conda base -> PATH 中的 python3
set "PY="
call :probe "%~dp0.venv\Scripts\python.exe"
if not defined PY (
    for /f "delims=" %%p in ('where python 2^>nul') do call :probe "%%p"
)
call :probe "%CONDA_PREFIX%\python.exe"
if not defined PY (
    for /f "delims=" %%p in ('where conda 2^>nul') do call :probe_conda "%%p"
)
if not defined PY (
    for /f "delims=" %%p in ('where python3 2^>nul') do call :probe "%%p"
)

set "PYTHONPATH=%~dp0src;%PYTHONPATH%"

:menu
echo.
echo ==========================================
echo   zCAISAgent 后端   http://%HOST%:%PORT%/
echo   1 = 启动    2 = 重启    0 = 停止    Q = 退出
echo ==========================================
set "input="
set /p "input=请输入: "
if "%input%"=="1" goto start
if "%input%"=="2" goto restart
if "%input%"=="0" goto stop
if /i "%input%"=="q" exit /b 0
if "%input%"=="" exit /b 0
echo 无效输入，请输入 1、2 或 0。
goto menu

:start
call :is_running
if not errorlevel 1 (
    echo [提示] 后端已在运行: http://%HOST%:%PORT%/
    goto menu
)
call :launch
if errorlevel 1 goto menu
echo [成功] 后端已启动: http://%HOST%:%PORT%/
goto menu

:restart
call :is_running
if errorlevel 1 (
    echo [提示] 后端未在运行，直接启动。
    call :launch
    if errorlevel 1 goto menu
    echo [成功] 后端已启动: http://%HOST%:%PORT%/
    goto menu
)
echo [重启] 正在停止后端...
call :kill_backend
if errorlevel 1 (
    echo [失败] 端口 %PORT% 仍被占用，请手动检查。
    goto menu
)
call :launch
if errorlevel 1 goto menu
echo [成功] 后端已重启: http://%HOST%:%PORT%/
goto menu

:stop
call :is_running
if errorlevel 1 (
    echo [提示] 后端未在运行。
    goto menu
)
echo [停止] 正在停止后端...
call :kill_backend
if errorlevel 1 (
    echo [失败] 端口 %PORT% 仍被占用，请手动检查。
    goto menu
)
echo [成功] 后端已停止。
goto menu

:launch
if not defined PY (
    echo [错误] 没有找到可用的 Python 环境（需要 uvicorn 与 click）。
    echo        已尝试: .venv、PATH 中的 python/python3、conda 环境
    echo        请先安装依赖后重试: python -m pip install -e .
    exit /b 1
)
if not exist ".zcaisagent" mkdir ".zcaisagent"
echo [启动] 正在启动后端，日志: %LOG%（标准错误）与 %LOG%.out（标准输出）
powershell -NoProfile -Command "Start-Process -FilePath '%PY%' -ArgumentList '-m','uvicorn','zcaisagent.api.app:app','--host','%HOST%','--port','%PORT%' -RedirectStandardOutput '%~dp0%LOG%.out' -RedirectStandardError '%~dp0%LOG%' -WindowStyle Hidden"
for /l %%i in (1,1,15) do (
    call :is_running
    if not errorlevel 1 exit /b 0
    ping -n 2 127.0.0.1 >nul
)
echo [失败] 15 秒内未监听 %PORT% 端口，请查看日志: %LOG%
exit /b 1

:kill_backend
powershell -NoProfile -Command "$p=(Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess); if ($p) { taskkill /PID $p /T /F | Out-Null }"
ping -n 2 127.0.0.1 >nul
call :is_running
if errorlevel 1 exit /b 0
exit /b 1

:probe
rem %~1 候选解释器路径；存在且能 import click/uvicorn 则记为 PY
if defined PY exit /b 0
if not exist "%~1" exit /b 0
"%~1" -c "import click, uvicorn" >nul 2>&1
if errorlevel 1 exit /b 0
set "PY=%~1"
exit /b 0

:probe_conda
rem %~1 形如 <conda base>\Scripts\conda.exe，取同级 base 目录下的 python.exe
if defined PY exit /b 0
call :probe "%~dp1..\python.exe"
exit /b 0

:is_running
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
exit /b
