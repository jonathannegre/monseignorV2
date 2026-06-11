#!/usr/bin/env python3
"""Read-only MonseignorV2 readiness report for fair competition vs MonseignorV1.

This script never submits, cancels, or repairs orders. It compares policy knobs,
account state, activation gates, and cron-offset metadata so V2 can be started
quickly once Jo gives the top départ.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from typing import Any

BASE = pathlib.Path(__file__).resolve().parents[1]
V1 = pathlib.Path('/home/hermes/projects/monseignor')


def load_json(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def pick(policy: dict[str, Any], dotted: str) -> Any:
    cur: Any = policy
    for part in dotted.split('.'):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def run_account_check() -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, 'scripts/check_alpaca_account.py'],
        cwd=BASE,
        text=True,
        capture_output=True,
        timeout=60,
    )
    try:
        data: dict[str, Any] = json.loads(proc.stdout or '{}')
    except json.JSONDecodeError:
        data = {'parse_error': proc.stdout[:500]}
    data['exit_code'] = proc.returncode
    if proc.stderr:
        data['stderr_tail'] = proc.stderr[-500:]
    return data


def main() -> int:
    v2 = load_json(BASE / 'config/policy.json')
    v1 = load_json(V1 / 'config/policy.json') if (V1 / 'config/policy.json').exists() else {}
    fields = [
        'risk_mode.max_risk_per_trade_pct',
        'risk_mode.max_total_exposure_pct',
        'risk_mode.max_open_positions',
        'risk_mode.min_confidence',
        'risk_mode.min_risk_reward',
        'market_scanner.top_n',
        'market_scanner.max_universe',
        'portfolio_construction.max_sector_exposure_pct',
        'portfolio_construction.max_new_orders',
        'catalyst_agent.finnhub_lookback_days',
    ]
    comparisons = []
    for field in fields:
        comparisons.append({
            'field': field,
            'monseignor_v1': pick(v1, field),
            'monseignor_v2': pick(v2, field),
            'matches': pick(v1, field) == pick(v2, field),
        })

    account = run_account_check()
    auth = v2.get('execution_authorization', {})
    readiness = v2.get('fair_competition_readiness', {})
    output = {
        'bot': 'MonseignorV2',
        'read_only': True,
        'trading_authorized': bool(auth.get('authorized_by_user') and auth.get('alpaca_paper_orders_after_full_pipeline')),
        'activation_state': readiness.get('activation_state'),
        'account_check': {
            'exit_code': account.get('exit_code'),
            'credentials_present': account.get('credentials_present'),
            'account_verified': account.get('account_verified'),
            'trading_blocked': account.get('trading_blocked'),
            'reason': account.get('reason'),
            'cash': account.get('cash'),
            'buying_power': account.get('buying_power'),
            'margin_multiplier': account.get('margin_multiplier'),
            'positions_count': len(account.get('positions') or []),
            'open_orders_count': len(account.get('open_orders') or []),
        },
        'policy_threshold_comparison': comparisons,
        'setup_rotation': {
            'source': pick(v2, 'setup_rotation.source'),
            'stats_count': len(pick(v2, 'setup_rotation.stats') or []),
            'boosted_setups': pick(v2, 'setup_rotation.boosted_setups'),
        },
        'launch_profile': pick(v2, 'portfolio_construction.launch_profile'),
        'expected_cron_offset': readiness.get('expected_cron_offset'),
        'next_activation_steps': [
            'force Alpaca Paper cash-only/no-shorting if still margin-configured',
            'run tests and dry-run account/executor smokes',
            'flip execution_authorization only after Jo top départ',
            'schedule trader at minutes 7,22,37,52 with deliver=local',
            'keep daily user report separate at 08:00 FR',
        ],
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
