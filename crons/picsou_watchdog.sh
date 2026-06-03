#!/usr/bin/env bash
set -euo pipefail
# Silent until Alpaca Paper credentials are configured. Never sends orders.
SECRET_FILE="/home/hermes/projects/monseignor/.secrets/alpaca-paper.env"
if [[ -f "$SECRET_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$SECRET_FILE"
  set +a
fi
if [[ -z "${APCA_API_KEY_ID:-${ALPACA_API_KEY:-}}" || -z "${APCA_API_SECRET_KEY:-${ALPACA_SECRET_KEY:-}}" ]]; then
  exit 0
fi
export APCA_API_BASE_URL="${APCA_API_BASE_URL:-https://paper-api.alpaca.markets}"
/home/hermes/projects/monseignor/scripts/daily_cycle.py
