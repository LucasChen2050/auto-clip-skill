@echo off
chcp 65001 >nul
title 全自动化剪辑 Skill
cd /d "%~dp0"
if exist "runtime\.ready" goto run
echo 首次运行，正在做准备（几秒钟）...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%~dp0' -Recurse -File | Unblock-File" >nul 2>nul
echo ok> "runtime\.ready"
:run
"runtime\python.exe" run.py
if errorlevel 1 pause
