#!/usr/bin/env bash
# Arranca el panel de Trading Sim en http://127.0.0.1:8777
set -e
cd "$(dirname "$0")"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8777}"
exec ./.venv/bin/python -m uvicorn backend.app:app --host "$HOST" --port "$PORT"
