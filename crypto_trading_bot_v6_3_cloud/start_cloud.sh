#!/bin/sh
set -eu
PORT="${PORT:-8501}"
export DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"
if [ "${WORKER_ENABLED:-1}" != "0" ]; then
  python worker.py &
fi
exec streamlit run cloud_app.py \
  --server.address=0.0.0.0 \
  --server.port="$PORT" \
  --server.headless=true \
  --browser.gatherUsageStats=false
