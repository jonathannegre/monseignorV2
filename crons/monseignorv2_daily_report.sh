#!/usr/bin/env bash
# MonseignorV2 daily WhatsApp report, user-facing only.
set -euo pipefail
cd /home/hermes/projects/monseignor-v2

SECRET_FILE=".secrets/alpaca-paper.env"
if [[ ! -f "$SECRET_FILE" ]]; then
  echo "🦄 *Hermes*"
  echo "〰️〰️〰️〰️"
  echo ""
  echo "MonseignorV2: credentials Alpaca Paper absents, rapport indisponible."
  exit 0
fi
set -a
# shellcheck disable=SC1090
source "$SECRET_FILE"
set +a
export APCA_API_BASE_URL="${APCA_API_BASE_URL:-https://paper-api.alpaca.markets}"

python3 - <<'PY'
import datetime as dt
import json
import os
import pathlib
import urllib.request

BASE = pathlib.Path('/home/hermes/projects/monseignor-v2')
policy = json.loads((BASE / 'config/policy.json').read_text())
api_key = os.environ['APCA_API_KEY_ID']
secret = os.environ['APCA_API_SECRET_KEY']
base_url = os.environ.get('APCA_API_BASE_URL', 'https://paper-api.alpaca.markets').rstrip('/')
headers = {'APCA-API-KEY-ID': api_key, 'APCA-API-SECRET-KEY': secret}

def get(path):
    req = urllib.request.Request(base_url + path, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())

acct = get('/v2/account')
positions = get('/v2/positions')
orders = get('/v2/orders?status=open')
portfolio = float(acct.get('portfolio_value') or acct.get('equity') or 0)
cash = float(acct.get('cash') or 0)
start = float(policy.get('competition_objectives', {}).get('starting_equity_usd') or policy.get('allocated_capital_usd') or 10000)
ret = ((portfolio - start) / start * 100) if start else 0.0
exposure = sum(float(p.get('market_value') or 0) for p in positions)
expo_pct = (exposure / portfolio * 100) if portfolio else 0.0
objectives = policy.get('competition_objectives', {}).get('objectives', [])
today = dt.date.today()

def objective_line(obj):
    target = float(obj.get('target_return_pct', 0))
    deadline = dt.date.fromisoformat(obj['deadline'])
    days = (deadline - today).days
    status = 'ATTEINT' if ret >= target else 'EN COURS'
    return f"{obj.get('name', target)}: {status} — deadline {deadline.isoformat()} ({days}j), écart {target-ret:+.2f} pts"

pos_lines = []
for p in sorted(positions, key=lambda x: float(x.get('market_value') or 0), reverse=True):
    pnl = float(p.get('unrealized_pl') or 0)
    pnl_pct = float(p.get('unrealized_plpc') or 0) * 100
    sign = '+' if pnl >= 0 else ''
    pos_lines.append(f"{p.get('symbol')}: qty {p.get('qty')} | MV {float(p.get('market_value') or 0):.2f}$ | P&L {sign}{pnl:.2f}$ ({sign}{pnl_pct:.1f}%)")

order_lines = []
for o in orders[:8]:
    order_lines.append(f"{o.get('symbol')} {o.get('side')} {o.get('qty')} @ {o.get('limit_price') or o.get('stop_price') or 'n/a'} — {o.get('status')}")

print('🦄 *Hermes*')
print('〰️〰️〰️〰️')
print('')
print(f"MonseignorV2 — rapport Alpaca Paper du {today.isoformat()}")
print('')
print(f"Portfolio: {portfolio:.2f}$ | Cash: {cash:.2f}$ | Exposition: {exposure:.2f}$ ({expo_pct:.1f}%)")
print(f"Performance depuis activation: {ret:+.2f}%")
for obj in objectives:
    print(objective_line(obj))
print('')
print(f"Positions ouvertes: {len(positions)}")
print('\n'.join(pos_lines) if pos_lines else 'Aucune position ouverte.')
print('')
print(f"Ordres ouverts: {len(orders)}")
print('\n'.join(order_lines) if order_lines else 'Aucun ordre ouvert.')
print('')
print(f"Mode: autonome agressif, paper-only/cash-only, max exposure {policy.get('max_total_exposure_pct')}%, max positions {policy.get('max_open_positions')}, risque/trade {policy.get('max_risk_per_trade_pct')}%")
PY
