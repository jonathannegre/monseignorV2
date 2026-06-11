#!/usr/bin/env python3
"""Performance attribution utilities for MonseignorV2."""
from __future__ import annotations

import json
import pathlib
from collections import defaultdict
from typing import Any, Mapping, Sequence


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def attribute_closed_trades(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, float]] = defaultdict(lambda: {'trades': 0.0, 'wins': 0.0, 'pnl': 0.0, 'r': 0.0})
    for t in trades:
        setup = str(t.get('setup') or t.get('strategy') or 'untracked')
        catalyst = str(t.get('catalyst_type') or t.get('catalyst_status') or 'unknown_catalyst')
        sector = str(t.get('sector') or 'Other')
        for key in (f'setup:{setup}', f'catalyst:{catalyst}', f'sector:{sector}'):
            g = groups[key]
            pnl = _f(t.get('pnl') or t.get('pnl_usd'))
            r = _f(t.get('realized_r') or t.get('r_multiple'))
            g['trades'] += 1
            g['wins'] += 1 if pnl > 0 else 0
            g['pnl'] += pnl
            g['r'] += r
    rows = []
    for key, g in groups.items():
        n = max(g['trades'], 1.0)
        rows.append({'bucket': key, 'trades': int(g['trades']), 'win_rate': round(g['wins'] / n, 3), 'total_pnl_usd': round(g['pnl'], 2), 'avg_r': round(g['r'] / n, 3)})
    rows.sort(key=lambda x: (x['total_pnl_usd'], x['avg_r']), reverse=True)
    return {'agent': 'Performance Attribution Agent', 'buckets': rows}


def extract_trade_records_from_journal(path: pathlib.Path) -> list[dict[str, Any]]:
    trades = []
    if not path.exists():
        return trades
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get('event_type') in {'trade_closed', 'position_exit', 'order_filled'}:
            data = event.get('data') if isinstance(event.get('data'), dict) else {}
            trades.append({**data, 'symbol': data.get('symbol') or event.get('symbol')})
    return trades
