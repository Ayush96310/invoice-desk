#!/usr/bin/env sh
# Starts Invoice Desk on http://localhost:8000
# Optional: export ANTHROPIC_API_KEY=sk-ant-... to enable AI extraction.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python -m uvicorn app.main:app --port 8000
