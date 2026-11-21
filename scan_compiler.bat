@echo off
setlocal
set "SCRIPT_DIR=%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [cdbg] 找不到 python，请先安装 Python 3 并加入 PATH
    pause
    exit /b 1
)

python "%SCRIPT_DIR%cdbg.py" --scan-compiler
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo [cdbg] 没有可用的工具链，退出码 %RC%
pause
endlocal
