@echo off
chcp 65001 >nul
title PACE
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PY (
  echo 没找到 Python，请先安装 Python 3.11 或以上。
  pause
  exit /b 1
)

echo 正在启动 PACE（http://127.0.0.1:8765/）……
echo 这个窗口关掉，服务就停了。重新打开网页前先双击本文件。
echo.
"%PY%" run.py --port 8765
echo.
echo 服务已退出。
pause
