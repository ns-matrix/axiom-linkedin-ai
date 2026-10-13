@echo off
setlocal
cd /d "%~dp0local-server"
if not exist ".venv\Scripts\python.exe" (
  echo First run:
  echo   py -m venv .venv
  echo   .venv\Scripts\activate
  echo   pip install -r requirements.txt
  pause
  exit /b 1
)
".venv\Scripts\python.exe" app.py
pause
