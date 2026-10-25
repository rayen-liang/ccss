#!/usr/bin/env bash
set -euo pipefail

URL_FILE="${CCSS_QUICK_TUNNEL_URL_FILE:-/var/lib/ccss/cloudflare-url.txt}"
LOCAL_URL="${CCSS_QUICK_TUNNEL_ORIGIN:-http://127.0.0.1:8501}"

if ! command -v cloudflared >/dev/null 2>&1; then
  printf 'cloudflared was not found on PATH.\n' >&2
  exit 1
fi

mkdir -p "$(dirname -- "$URL_FILE")"
temporary_url_file="${URL_FILE}.tmp"
rm -f -- "$URL_FILE" "$temporary_url_file"
trap 'rm -f -- "$URL_FILE" "$temporary_url_file"' EXIT

cloudflared tunnel --no-autoupdate --url "$LOCAL_URL" 2>&1 |
  while IFS= read -r line; do
    printf '%s\n' "$line"
    if [[ "$line" =~ (https://[[:alnum:]-]+\.trycloudflare\.com) ]]; then
      printf '%s\n' "${BASH_REMATCH[1]}" > "$temporary_url_file"
      mv -f -- "$temporary_url_file" "$URL_FILE"
    fi
  done
