@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 缺少虚拟环境。请在此目录执行: python -m venv .venv
  pause
  exit /b 1
)
".venv\Scripts\python.exe" run.py
if errorlevel 1 pause
