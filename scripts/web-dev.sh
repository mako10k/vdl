#!/usr/bin/env bash
# Run the vdl FastAPI server and Vite console together for local development.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON:-python}"
API_HOST="${VDL_API_HOST:-127.0.0.1}"
API_PORT="${VDL_API_PORT:-18765}"
WEB_HOST="${VDL_WEB_HOST:-127.0.0.1}"
WEB_PORT="${VDL_WEB_PORT:-18766}"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm が見つかりません。Node.js をインストールしてから再実行してください。" >&2
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import uvicorn' >/dev/null 2>&1; then
  echo "uvicorn が見つかりません。プロジェクトルートで pip install -e \".[web]\" を実行してください。" >&2
  exit 1
fi

cd "$ROOT_DIR"
echo "API  http://${API_HOST}:${API_PORT}"
echo "UI   http://${WEB_HOST}:${WEB_PORT}"
echo "停止: Ctrl+C"

api_pid=""
if "$PYTHON_BIN" - "$API_HOST" "$API_PORT" <<'PY'
import json
import sys
from urllib.request import urlopen

host, port = sys.argv[1:]
try:
    with urlopen(f"http://{host}:{port}/api/health", timeout=0.5) as response:
        payload = json.load(response)
    if payload.get("status") != "ok":
        raise ValueError("not a vdl API")
except Exception:
    sys.exit(1)
PY
then
  echo "API はすでに起動しています。既存プロセスを再利用します。"
else
  "$PYTHON_BIN" -m uvicorn api.main:app \
    --reload \
    --host "$API_HOST" \
    --port "$API_PORT" &
  api_pid=$!
fi

(
  cd "$ROOT_DIR/web"
  npm run dev -- --host "$WEB_HOST" --port "$WEB_PORT" --strictPort
) &
web_pid=$!

cleanup() {
  trap - EXIT INT TERM
  kill "$web_pid" 2>/dev/null || true
  if [[ -n "$api_pid" ]]; then
    kill "$api_pid" 2>/dev/null || true
  fi
  wait "$web_pid" 2>/dev/null || true
  if [[ -n "$api_pid" ]]; then
    wait "$api_pid" 2>/dev/null || true
  fi
}

trap cleanup EXIT
trap 'exit 130' INT TERM

wait_pids=("$web_pid")
if [[ -n "$api_pid" ]]; then
  wait_pids+=("$api_pid")
fi

set +e
wait -n "${wait_pids[@]}"
status=$?
set -e
exit "$status"
