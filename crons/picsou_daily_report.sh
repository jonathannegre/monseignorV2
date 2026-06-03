#!/bin/bash
# Picsou daily report - runs at 08:00 CEST, delivers via WhatsApp
set -euo pipefail
cd /home/hermes/projects/monseignor
source .secrets/alpaca-paper.env

ACCT=$(curl -s -H "APCA-API-KEY-ID: $APCA_API_KEY_ID" -H "APCA-API-SECRET-KEY: $APCA_API_SECRET_KEY" https://paper-api.alpaca.markets/v2/account)
POSITIONS=$(curl -s -H "APCA-API-KEY-ID: $APCA_API_KEY_ID" -H "APCA-API-SECRET-KEY: $APCA_API_SECRET_KEY" https://paper-api.alpaca.markets/v2/positions)
ORDERS=$(curl -s -H "APCA-API-KEY-ID: $APCA_API_KEY_ID" -H "APCA-API-SECRET-KEY: $APCA_API_SECRET_KEY" "https://paper-api.alpaca.markets/v2/orders?status=open")

PORTFOLIO=$(echo "$ACCT" | python3 -c "import sys,json; print(json.load(sys.stdin)['portfolio_value'])")
CASH=$(echo "$ACCT" | python3 -c "import sys,json; print(json.load(sys.stdin)['cash'])")

INITIAL=10000
RETURN_PCT=$(python3 -c "print(f'{($PORTFOLIO - $INITIAL) / $INITIAL * 100:.2f}')")

N_POS=$(echo "$POSITIONS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
N_ORD=$(echo "$ORDERS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

POS_DETAILS=$(echo "$POSITIONS" | python3 -c "
import sys, json
positions = json.load(sys.stdin)
for p in positions:
    pnl = float(p['unrealized_pl'])
    pnl_pct = float(p['unrealized_plpc']) * 100
    sign = '+' if pnl >= 0 else ''
    print(f'  {p[\"symbol\"]}: {p[\"qty\"]} @ {p[\"avg_entry_price\"]} | {sign}{pnl:.2f} USD ({sign}{pnl_pct:.1f}%)')
")

DAYS_LEFT=$(python3 -c "from datetime import date; print((date(2026,5,30) - date.today()).days)")
TARGET_10=$(python3 -c "print('ATTEINT' if float('$RETURN_PCT') >= 10 else 'EN COURS')")
TARGET_20=$(python3 -c "print('ATTEINT' if float('$RETURN_PCT') >= 20 else 'EN COURS')")

echo "PICSOU DAILY REPORT $(date +%Y-%m-%d)"
echo ""
echo "Portfolio: $PORTFOLIO USD"
echo "Cash libre: $CASH USD"
echo "Rendement: ${RETURN_PCT}%"
echo "Objectif 10%: $TARGET_10 | 20%: $TARGET_20"
echo "Jours restants: $DAYS_LEFT"
echo ""
echo "Positions ouvertes: $N_POS"
echo "$POS_DETAILS"
echo ""
echo "Ordres ouverts: $N_ORD"
echo ""
echo "Mode: autonome agressif"
echo "Risque/trade: 2% | Expo max: 80% | Positions max: 10"
