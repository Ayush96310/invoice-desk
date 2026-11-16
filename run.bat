@echo off
REM Starts Invoice Desk on http://localhost:8000
REM Optional: set ANTHROPIC_API_KEY=sk-ant-... before running to enable AI extraction.
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python -m uvicorn app.main:app --port 8000
