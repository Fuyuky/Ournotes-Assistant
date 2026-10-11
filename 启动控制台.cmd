@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\pythonw.exe" (
  echo Python environment missing. Run uv sync --group browser first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
  echo WebUI dependencies missing. Run uv sync --group browser first.
  pause
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" -X utf8 "%~dp0scripts\webui.py"
