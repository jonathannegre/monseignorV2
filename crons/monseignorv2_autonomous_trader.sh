#!/usr/bin/env bash
# MonseignorV2 autonomous Alpaca Paper trader. Local/silent cron; user-facing reporting is separate.
set -euo pipefail
cd /home/hermes/projects/monseignor-v2

SECRET_FILE=".secrets/alpaca-paper.env"
if [[ ! -f "$SECRET_FILE" ]]; then
  exit 0
fi
set -a
# shellcheck disable=SC1090
source "$SECRET_FILE"
if [[ -f .secrets/finnhub.env ]]; then
  # shellcheck disable=SC1091
  source .secrets/finnhub.env
fi
set +a
export APCA_API_BASE_URL="${APCA_API_BASE_URL:-https://paper-api.alpaca.markets}"

# Never run outside Alpaca Paper.
if [[ "$APCA_API_BASE_URL" != "https://paper-api.alpaca.markets" ]]; then
  echo "MonseignorV2 refused: APCA_API_BASE_URL is not Alpaca Paper" >&2
  exit 3
fi

CLOCK=$(curl -fsS -H "APCA-API-KEY-ID: $APCA_API_KEY_ID" -H "APCA-API-SECRET-KEY: $APCA_API_SECRET_KEY" https://paper-api.alpaca.markets/v2/clock)
IS_OPEN=$(python3 -c "import json, sys; print(json.load(sys.stdin).get('is_open') is True)" <<<"$CLOCK")
if [[ "$IS_OPEN" != "True" ]]; then
  exit 0
fi

python3 scripts/daily_cycle.py

# Version only deterministic code/config changes, never journal/reports/data/secrets.
if [[ -n "$(git status --porcelain -- scripts/ config/ crons/ tests/ docs/)" ]]; then
  git add scripts/ config/ crons/ tests/ docs/
  git commit -m "auto: monseignorv2 cycle update $(date +%Y-%m-%d_%H:%M)" --quiet || true
  git push origin main --quiet 2>/dev/null || true
fi
