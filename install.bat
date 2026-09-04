@echo off
rem One-time setup: virtualenv + dependencies + a local LLM config seed.
echo Creating virtualenv...
py -m venv .venv || python -m venv .venv
call .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
rem Optional: rendercv toolchain for 1-page Harvard PDFs (needs Python 3.12+).
rem Skipped automatically if you only have an older Python - everything else still works.
py -3 -m venv .venv-rendercv 2>nul || python -m venv .venv-rendercv 2>nul
if exist .venv-rendercv\Scripts\python.exe (
  echo Installing rendercv PDF toolchain...
  .venv-rendercv\Scripts\python.exe -m pip install -q "rendercv[full]"
) else (
  echo. & echo Note: no Python 3.12+ found - the 1-page Harvard CV builder will be disabled.
)
if not exist config.local.json (
  echo.
  echo Created config.local.json - edit it to enable AI resume tailoring (optional).
  copy config.local.json.example config.local.json
)
echo.
echo Setup done. Run run.bat to start the dashboard at http://127.0.0.1:8371
pause
