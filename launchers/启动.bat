@echo off
chcp 65001 >nul
title 全自动化剪辑 Skill
cd /d "%~dp0"
rem 屏蔽电脑上其他 Python 的干扰（用户目录里的包、PYTHONHOME 等环境变量）
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
if not exist "runtime\Lib\encodings\__init__.py" goto broken
if exist "runtime\.ready" goto run
echo 首次运行，正在做准备（几秒钟）...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%~dp0' -Recurse -File | Unblock-File" >nul 2>nul
echo ok> "runtime\.ready"
:run
"runtime\python.exe" -s -E run.py
if errorlevel 1 pause
goto :eof
:broken
echo.
echo  [!] 工具文件不完整，无法启动。常见原因：
echo      1. 没有完整解压：请右键压缩包 - 全部解压缩，再打开解压出来的文件夹运行；
echo      2. 放在了 OneDrive 同步的桌面/文档里：OneDrive 只下载了一部分文件。
echo.
echo  解决办法：把压缩包复制到 D 盘或 C:\工具 这类普通文件夹（不要放桌面、文档、OneDrive），
echo  重新“全部解压缩”，然后双击里面的“启动.bat”。
echo.
echo  当前位置：%~dp0
echo.
pause
