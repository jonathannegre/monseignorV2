#!/usr/bin/env python3
"""Broker-visible protective stop audit and repair planning for MonseignorV2."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import urllib.request
from typing import Any, Mapping, Sequence


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ''):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _headers() -> dict[str, str]:
    return {
        'APCA-API-KEY-ID': os.getenv('APCA_API_KEY_ID') or os.getenv('ALPACA_API_KEY') or '',
        'APCA-API-SECRET-KEY': os.getenv('APCA_API_SECRET_KEY') or os.getenv('ALPACA_SECRET_KEY') or '',
    }


def _base_url() -> str:
    return (os.getenv('APCA_API_BASE_URL') or 'https://paper-api.alpaca.markets').rstrip('/')


def _get_json(path: str) -> Any:
    req = urllib.request.Request(_base_url() + path, headers=_headers(), method='GET')
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _post_json(path: str, body: Mapping[str, Any]) -> Any:
    req = urllib.request.Request(_base_url() + path, data=json.dumps(dict(body)).encode('utf-8'), headers={**_headers(), 'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode('utf-8'))


@dataclasses.dataclass(frozen=True)
class StopAuditRow:
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    has_stop: bool
    stop_price: float
    desired_stop_price: float
    status: str
    repair_payload: dict[str, Any] | None = None


def _symbol_stop_orders(open_orders: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    stops: dict[str, list[Mapping[str, Any]]] = {}
    for order in open_orders:
        side = str(order.get('side', '')).lower()
        typ = str(order.get('type') or order.get('order_type') or '').lower()
        order_class = str(order.get('order_class') or '').lower()
        symbol = str(order.get('symbol', '')).upper()
        if side == 'sell' and symbol and (typ == 'stop' or 'stop' in order_class):
            stops.setdefault(symbol, []).append(order)
    return stops


def desired_stop_price(position: Mapping[str, Any], policy: Mapping[str, Any], setup_plan: Mapping[str, Any] | None = None) -> float:
    setup_plan = setup_plan or {}
    entry = _f(position.get('avg_entry_price') or setup_plan.get('entry'))
    current = _f(position.get('current_price') or position.get('market_price') or entry)
    configured = _f(setup_plan.get('stop_loss') or setup_plan.get('stop_loss_price'))
    if configured > 0:
        base = configured
    else:
        risk_pct = _f(policy.get('position_manager', {}).get('default_stop_pct'), 3.0)
        base = entry * (1.0 - risk_pct / 100.0)
    breakeven_after = _f(policy.get('position_manager', {}).get('breakeven_after_r'), 1.0)
    initial_risk = max(entry - base, entry * 0.005, 0.01)
    open_r = (current - entry) / initial_risk if initial_risk > 0 else 0.0
    if open_r >= breakeven_after:
        base = max(base, entry)
    return round(min(base, current * 0.995), 2) if current > 0 else round(base, 2)


def build_stop_payload(symbol: str, qty: float, stop_price: float) -> dict[str, Any]:
    tif = 'day' if abs(qty - round(qty)) > 0.0001 else 'gtc'
    return {'symbol': symbol, 'qty': str(round(qty, 4) if tif == 'day' else int(round(qty))), 'side': 'sell', 'type': 'stop', 'time_in_force': tif, 'stop_price': str(round(stop_price, 2))}


def audit_protective_stops(positions: Sequence[Mapping[str, Any]], open_orders: Sequence[Mapping[str, Any]], policy: Mapping[str, Any], setup_plans: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    setup_plans = setup_plans or {}
    stop_orders = _symbol_stop_orders(open_orders)
    rows: list[StopAuditRow] = []
    missing = 0
    stale = 0
    for pos in positions:
        symbol = str(pos.get('symbol', '')).upper()
        qty = abs(_f(pos.get('qty')))
        if not symbol or qty <= 0:
            continue
        desired = desired_stop_price(pos, policy, setup_plans.get(symbol))
        live = stop_orders.get(symbol, [])
        live_stop = max((_f(o.get('stop_price') or (o.get('stop_loss', {}) if isinstance(o.get('stop_loss'), Mapping) else {}).get('stop_price')) for o in live), default=0.0)
        has_stop = live_stop > 0
        if not has_stop:
            status = 'missing_stop_repair_required'; missing += 1
        elif live_stop < desired * 0.985:
            status = 'stale_stop_too_loose_repair_required'; stale += 1
        else:
            status = 'protected'
        repair = None if status == 'protected' else build_stop_payload(symbol, qty, desired)
        rows.append(StopAuditRow(symbol, qty, _f(pos.get('avg_entry_price')), _f(pos.get('current_price') or pos.get('market_price')), has_stop, round(live_stop, 2), desired, status, repair))
    return {'agent': 'Broker Stop Audit Agent', 'positions_checked': len(rows), 'missing_stop_count': missing, 'stale_stop_count': stale, 'all_positions_protected': missing == 0 and stale == 0, 'rows': [dataclasses.asdict(r) for r in rows], 'critical_incident': missing > 0 or stale > 0}


def live_audit(policy: Mapping[str, Any]) -> dict[str, Any]:
    if 'paper-api.alpaca.markets' not in _base_url():
        return {'agent': 'Broker Stop Audit Agent', 'critical_incident': True, 'reason': 'not_paper_endpoint'}
    if not (_headers().get('APCA-API-KEY-ID') and _headers().get('APCA-API-SECRET-KEY')):
        return {'agent': 'Broker Stop Audit Agent', 'critical_incident': False, 'reason': 'no_credentials'}
    positions = _get_json('/v2/positions')
    orders = _get_json('/v2/orders?status=open&nested=true')
    if not isinstance(positions, list): positions = []
    if not isinstance(orders, list): orders = []
    audit = audit_protective_stops(positions, orders, policy)
    audit['checked_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    return audit


def repair_missing_stops(audit: Mapping[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
    submitted: list[dict[str, Any]] = []
    for row in audit.get('rows', []):
        if not isinstance(row, Mapping) or row.get('status') == 'protected':
            continue
        payload = row.get('repair_payload')
        if not isinstance(payload, Mapping):
            continue
        if dry_run:
            submitted.append({'symbol': row.get('symbol'), 'status': 'dry_run_repair_planned', 'payload': dict(payload)})
        else:
            resp = _post_json('/v2/orders', payload)
            submitted.append({'symbol': row.get('symbol'), 'status': 'submitted', 'order_id': resp.get('id'), 'broker_status': resp.get('status')})
    return {'agent': 'Broker Stop Repair Agent', 'dry_run': dry_run, 'repair_orders': submitted, 'orders_sent': 0 if dry_run else len(submitted)}
