#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"
umask 077

if [[ -n "${CCSS_DATA_DIR:-}" ]]; then
  export CCSS_DB_PATH="${CCSS_DB_PATH:-$CCSS_DATA_DIR/ccss.db}"
  export CCSS_BACKUP_DIR="${CCSS_BACKUP_DIR:-$CCSS_DATA_DIR/backups}"
fi

if [[ -n "${STREAMLIT_BIN:-}" ]]; then
  if [[ ! -x "$STREAMLIT_BIN" ]]; then
    printf 'STREAMLIT_BIN is not executable: %s\n' "$STREAMLIT_BIN" >&2
    exit 1
  fi
elif [[ -x "$APP_DIR/.venv/bin/streamlit" ]]; then
  STREAMLIT_BIN="$APP_DIR/.venv/bin/streamlit"
elif command -v streamlit >/dev/null 2>&1; then
  STREAMLIT_BIN="$(command -v streamlit)"
else
  printf 'Streamlit was not found. Install it or set STREAMLIT_BIN.\n' >&2
  exit 1
fi

exec "$STREAMLIT_BIN" run "$APP_DIR/streamlit_app.py" \
  --server.address "${CCSS_SERVER_ADDRESS:-0.0.0.0}" \
  --server.port "${CCSS_SERVER_PORT:-8501}" \
  "$@"
