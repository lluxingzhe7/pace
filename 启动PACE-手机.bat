@echo off
chcp 65001 >nul
title PACE - 手机访问（同一 WiFi）
cd /d "%~dp0"

set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================
echo   PACE - 手机访问模式
echo ============================================
echo.
echo   1. 手机和这台电脑连同一个 WiFi
echo   2. 看下面打印的「手机」那一行地址
echo   3. 手机浏览器打开它，可以「添加到主屏幕」
echo.
echo   电脑不关、这个窗口不关，手机才能访问。
echo ============================================
echo.

"%PY%" run.py --host 0.0.0.0 --port 8765
pause
