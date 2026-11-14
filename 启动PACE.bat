@echo off
chcp 65001 >nul
title PACE - Sports Analytics
cd /d "%~dp0"

rem 用 %USERPROFILE% 拼路径，避免脚本文件编码把中文用户名写坏
set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================
echo   PACE  -  local sports analytics
echo ============================================
echo.
echo   Starting server, browser will open shortly.
echo   Keep this window open while using PACE.
echo   Close this window to stop the server.
echo.
echo ============================================
echo.

"%PY%" run.py --port 8765
if errorlevel 1 (
  echo.
  echo [ERROR] Server exited. Check the message above.
  pause
)
