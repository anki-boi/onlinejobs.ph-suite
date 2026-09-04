@echo off
if not exist .venv (
  echo .venv not found - run install.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate
python main.py
pause
