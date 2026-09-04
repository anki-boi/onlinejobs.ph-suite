@echo off
rem One-time setup: virtualenv + dependencies + a local LLM config seed.
echo Creating virtualenv...
py -m venv .venv || python -m venv .venv
call .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if not exist config.local.json (
  echo.
  echo Created config.local.json - edit it to enable AI resume tailoring (optional).
  copy config.local.json.example config.local.json
)
echo.
echo Setup done. Run run.bat to start the dashboard at http://127.0.0.1:8371
pause
