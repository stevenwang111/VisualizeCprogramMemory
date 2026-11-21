@echo off
rem ============================================================
rem  把 .c 文件的「打开方式」注册为 cdbg
rem
rem  右键任意 .c 文件 → 打开方式 → 选「cdbg」→ 勾选「始终使用」
rem
rem  需要管理员权限运行本脚本（改 HKCU 不需要，改 HKLM 需要）。
rem  本脚本改的是 HKCU（当前用户），一般直接双击即可。
rem ============================================================
setlocal

set "BAT=%~dp0cdbg.bat"
if not exist "%BAT%" (
    echo 找不到 %BAT%
    pause
    exit /b 1
)

reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.c\UserChoice" /v "ProgId" /t REG_SZ /d "cdbg.SourceFile" /f >nul 2>&1
reg add "HKCU\Software\Classes\cdbg.SourceFile\DefaultIcon" /ve /t REG_SZ /d "%BAT%,0" /f >nul
reg add "HKCU\Software\Classes\cdbg.SourceFile\shell\open\command" /ve /t REG_SZ /d "\"%BAT%\" \"%1\"" /f >nul

echo.
echo 已注册：现在右键 .c 文件 →「打开方式」应该能看到 cdbg。
echo 若没有出现在列表里，可在「选择其它应用」里手动指定：
echo     %BAT%
echo.
pause
endlocal
