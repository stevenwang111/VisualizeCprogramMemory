@echo off
rem ============================================================
rem  cdbg 启动器 —— 供 Windows「打开方式」和拖拽使用
rem
rem  用法：
rem    把 .c 文件拖到本文件上          → 用 cdbg 调试它
rem    在资源管理器里右键 → 打开方式 → cdbg
rem ============================================================
setlocal

rem 定位到 cdbg.py 所在目录（本脚本与 cdbg.py 同级或在其上级）
set "SCRIPT_DIR=%~dp0"

rem 优先用本目录的 cdbg.py，否则用 PATH 里的 cdbg
if exist "%SCRIPT_DIR%cdbg.py" (
    set "CDBG_PY=%SCRIPT_DIR%cdbg.py"
) else (
    set "CDBG_PY=cdbg"
)

rem 允许传入多个文件（拖拽多选），这里只处理第一个
if "%~1"=="" (
    echo 用法：把 .c 源文件拖到本文件上，或右键选择「打开方式」
    pause
    exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
    echo [cdbg] 找不到 python，请先安装 Python 3 并加入 PATH
    pause
    exit /b 1
)

python "%CDBG_PY%" "%~1"
if errorlevel 1 pause
endlocal
