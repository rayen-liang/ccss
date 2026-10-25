#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DATA_DIR="/var/lib/ccss"
SERVICE_USER="${CCSS_SERVICE_USER:-${SUDO_USER:-}}"
SERVICE_HOME=""
STREAMLIT_BIN="${APP_DIR}/.venv/bin/streamlit"
SOURCE_DB="${CCSS_SOURCE_DB_PATH:-$APP_DIR/ccss.db}"

if [[ "$EUID" -ne 0 ]]; then
  printf 'Run this installer with sudo, for example: sudo %s\n' "$0" >&2
  exit 1
fi

if [[ -z "$SERVICE_USER" || "$SERVICE_USER" == "root" ]]; then
  printf 'Set CCSS_SERVICE_USER to a non-root Linux account that owns the app.\n' >&2
  exit 1
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  printf 'Linux user does not exist: %s\n' "$SERVICE_USER" >&2
  exit 1
fi
SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
if [[ ! -f "$APP_DIR/streamlit_app.py" || ! -f "$APP_DIR/run_linux.sh" ]]; then
  printf 'Run this script from the CCSS project copy; streamlit_app.py or run_linux.sh is missing.\n' >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  printf 'systemd is required to install background services.\n' >&2
  exit 1
fi
if ! command -v cloudflared >/dev/null 2>&1; then
  printf 'Install cloudflared first: https://developers.cloudflare.com/tunnel/downloads/\n' >&2
  exit 1
fi

if [[ ! -x "$STREAMLIT_BIN" ]]; then
  if [[ -x "$SERVICE_HOME/.local/bin/streamlit" ]]; then
    STREAMLIT_BIN="$SERVICE_HOME/.local/bin/streamlit"
  else
    STREAMLIT_BIN="$(command -v streamlit || true)"
  fi
fi
if [[ -z "$STREAMLIT_BIN" || ! -x "$STREAMLIT_BIN" ]]; then
  printf 'Streamlit was not found. Install it in %s/.venv or on the system PATH.\n' "$APP_DIR" >&2
  exit 1
fi

if [[ "$APP_DIR" == *$'\n'* || "$APP_DIR" == *%* ]]; then
  printf 'The project path cannot contain a newline or percent sign for systemd units.\n' >&2
  exit 1
fi

systemd_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"' "$value"
}

APP_DIR_UNIT="$(systemd_quote "$APP_DIR")"
STREAMLIT_BIN_UNIT="$(systemd_quote "$STREAMLIT_BIN")"
SERVICE_HOME_UNIT="$(systemd_quote "$SERVICE_HOME")"

install -d -o "$SERVICE_USER" -g "$(id -gn "$SERVICE_USER")" -m 0750 "$DATA_DIR"
install -d -o "$SERVICE_USER" -g "$(id -gn "$SERVICE_USER")" -m 0750 "$DATA_DIR/backups"
chmod 0755 "$APP_DIR/run_linux.sh" "$APP_DIR/scripts/cloudflare_quick_tunnel.sh"

if [[ ! -e "$DATA_DIR/ccss.db" && -f "$SOURCE_DB" ]]; then
  PYTHON_BIN="${APP_DIR}/.venv/bin/python"
  if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3 || true)"
  fi
  if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
    printf 'Python 3 is required to safely copy the existing SQLite database.\n' >&2
    exit 1
  fi
  "$PYTHON_BIN" - "$SOURCE_DB" "$DATA_DIR/ccss.db" <<'PY'
import os
import sqlite3
import sys

source_path, destination_path = sys.argv[1:]
temporary_path = f"{destination_path}.migrating"
try:
    with sqlite3.connect(source_path) as source:
        with sqlite3.connect(temporary_path) as destination:
            source.backup(destination)
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, destination_path)
finally:
    if os.path.exists(temporary_path):
        os.unlink(temporary_path)
PY
  printf 'Copied existing SQLite data from %s; the source file was not changed.\n' "$SOURCE_DB"
fi

cat > /etc/systemd/system/ccss-streamlit.service <<EOF
[Unit]
Description=CCSS Streamlit application
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Environment=HOME=$SERVICE_HOME_UNIT
WorkingDirectory=$APP_DIR_UNIT
Environment=CCSS_DATA_DIR=$DATA_DIR
Environment=CCSS_SERVER_ADDRESS=127.0.0.1
Environment=CCSS_SERVER_PORT=8501
Environment=STREAMLIT_BIN=$STREAMLIT_BIN_UNIT
UMask=0077
ExecStart=/bin/bash $APP_DIR_UNIT/run_linux.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/ccss-cloudflare.service <<EOF
[Unit]
Description=CCSS Cloudflare Quick Tunnel (temporary public URL)
Wants=network-online.target
After=network-online.target ccss-streamlit.service
BindsTo=ccss-streamlit.service

[Service]
Type=simple
User=$SERVICE_USER
Environment=HOME=$SERVICE_HOME_UNIT
WorkingDirectory=$APP_DIR_UNIT
Environment=CCSS_QUICK_TUNNEL_URL_FILE=$DATA_DIR/cloudflare-url.txt
Environment=CCSS_QUICK_TUNNEL_ORIGIN=http://127.0.0.1:8501
UMask=0077
ExecStart=/bin/bash $APP_DIR_UNIT/scripts/cloudflare_quick_tunnel.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

chown "$SERVICE_USER:$(id -gn "$SERVICE_USER")" "$DATA_DIR" "$DATA_DIR/backups"
if [[ -f "$DATA_DIR/ccss.db" ]]; then
  chown "$SERVICE_USER:$(id -gn "$SERVICE_USER")" "$DATA_DIR/ccss.db"
  chmod 0600 "$DATA_DIR/ccss.db"
fi
systemctl daemon-reload
systemctl enable --now ccss-streamlit.service ccss-cloudflare.service

printf 'Waiting for Cloudflare Quick Tunnel URL'
for _ in {1..90}; do
  if [[ -s "$DATA_DIR/cloudflare-url.txt" ]]; then
    printf '\nCCSS public URL: '
    cat "$DATA_DIR/cloudflare-url.txt"
    printf '\nURL file: %s/cloudflare-url.txt\n' "$DATA_DIR"
    printf 'Services are installed and will keep running after you disconnect.\n'
    printf 'View logs: sudo journalctl -u ccss-streamlit -u ccss-cloudflare -f\n'
    exit 0
  fi
  if ! systemctl is-active --quiet ccss-cloudflare.service; then
    break
  fi
  printf '.'
  sleep 1
done

printf '\nCould not obtain a Quick Tunnel URL. Recent Cloudflare logs:\n' >&2
journalctl -u ccss-cloudflare.service -n 40 --no-pager >&2 || true
exit 1