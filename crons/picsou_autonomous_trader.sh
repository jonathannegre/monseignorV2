#!/bin/bash
# Picsou autonomous trader - runs every 15min during market hours
# Scans, analyzes, and executes trades autonomously
set -euo pipefail
cd /home/hermes/projects/picsou-alpaca
set -a
source .secrets/alpaca-paper.env
set +a

# Check if market is open
CLOCK=$(curl -s -H "APCA-API-KEY-ID: $APCA_API_KEY_ID" -H "APCA-API-SECRET-KEY: $APCA_API_SECRET_KEY" https://paper-api.alpaca.markets/v2/clock)
IS_OPEN=$(echo "$CLOCK" | python3 -c "import sys,json; print(json.load(sys.stdin)['is_open'])")

if [ "$IS_OPEN" != "True" ]; then
    # Silent when market closed
    exit 0
fi

# Run the full autonomous cycle
python3 scripts/daily_cycle.py 2>&1
