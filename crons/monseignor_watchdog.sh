#!/usr/bin/env bash
# Monseignor watchdog. Silent when healthy; emits only actionable problems.
set -euo pipefail
cd /home/hermes/projects/monseignor

SECRET_FILE=".secrets/alpaca-paper.env"
if [[ ! -f "$SECRET_FILE" ]]; then
  exit 0
fi
set -a
# shellcheck disable=SC1090
source "$SECRET_FILE"
set +a
export APCA_API_BASE_URL="${APCA_API_BASE_URL:-https://paper-api.alpaca.markets}"

OUT=$(python3 scripts/check_alpaca_account.py 2>&1) || CODE=$? || true
CODE=${CODE:-0}
if [[ "$CODE" -ne 0 ]]; then
  printf '%s\n' "$OUT" | python3 -c "import json,sys; data=json.loads(sys.stdin.read()); print('Monseignor watchdog: trading blocked/account check failed. reason=%s cash=%s buying_power=%s multiplier=%s' % (data.get('reason'), data.get('cash'), data.get('buying_power'), data.get('margin_multiplier')))" \
    || echo "Monseignor watchdog: account check failed: ${OUT:0:500}"
fi
