#!/usr/bin/env python3
import datetime as dt
import json
import pathlib
import subprocess
import sys
from typing import Any, Dict

try:
    from .market_scanner import run_live_scan
    from .trade_validation_pipeline import append_trade_validation_journal, compose_trade_validation_pipeline
    from .order_executor import execute_proposals
except ImportError:  # script execution: python3 scripts/daily_cycle.py
    SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from market_scanner import run_live_scan
    from trade_validation_pipeline import append_trade_validation_journal, compose_trade_validation_pipeline
    from order_executor import execute_proposals

BASE = pathlib.Path('/home/hermes/projects/picsou-alpaca')
JOURNAL = BASE / 'journal' / 'events.jsonl'
REPORTS = BASE / 'reports'
POLICY_PATH = BASE / 'config' / 'policy.json'


def load_policy():
    with POLICY_PATH.open(encoding='utf-8') as f:
        return json.load(f)


def save_policy(policy: Dict[str, Any]) -> None:
    POLICY_PATH.write_text(json.dumps(policy, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def run_check():
    p = subprocess.run([sys.executable, str(BASE / 'scripts' / 'check_alpaca_account.py')], text=True, capture_output=True, timeout=30)
    try:
        data = json.loads(p.stdout.strip() or '{}')
    except Exception:
        data = {'account_verified': False, 'trading_blocked': True, 'reason': 'invalid_check_output'}
    return p.returncode, data


def _as_float(value):
    return float(value or 0)


def _effective_capital(account, policy):
    allocation_cap = _as_float(policy.get('allocated_capital_usd', 0))
    broker_cash = _as_float(account.get('cash', 0))
    broker_equity = _as_float(account.get('portfolio_value', broker_cash))
    if allocation_cap > 0:
        return min(broker_equity, allocation_cap), min(broker_cash, allocation_cap)
    return broker_equity, broker_cash


def build_summary(account, policy=None, manual_validation=False, trade_validation=None, market_scan=None):
    policy = policy or load_policy()
    if not manual_validation:
        manual_validation = bool(
            policy.get('execution_authorization', {}).get('alpaca_paper_orders_after_full_pipeline', False)
        )
    watchlist = None
    if market_scan:
        watchlist = market_scan.get('market_scanner', {}).get('candidates')
    if trade_validation is None:
        trade_validation = compose_trade_validation_pipeline(
            policy,
            account,
            manual_validation=manual_validation,
            watchlist=watchlist,
        )

    trading_blocked = bool(account.get('trading_blocked', True))
    credentials_present = bool(account.get('credentials_present', False))
    opportunities_found = trade_validation.get('proposals_count', 0)
    if market_scan:
        opportunities_found = int(market_scan.get('market_scanner', {}).get('opportunities_found', opportunities_found) or 0)

    if not credentials_present:
        next_action = 'Fournir clés Alpaca Paper'
    elif trading_blocked:
        next_action = 'Corriger incohérence compte avant tout scan/exécution'
    else:
        next_action = 'Compte vérifié; scan prudent possible'

    effective_portfolio_value, effective_cash = _effective_capital(account, policy)
    summary = {
        'cycle_summary': {
            'portfolio_value': effective_portfolio_value,
            'available_cash': effective_cash,
            'buying_power': _as_float(account.get('buying_power', account.get('cash', 0))),
            'open_positions': int(account.get('open_positions_count', 0) or 0),
            'pending_orders': int(account.get('open_orders_count', 0) or 0),
            'market_mode': 'risk_off' if trading_blocked else 'neutral',
            'opportunities_found': opportunities_found,
            'trades_approved': trade_validation.get('trades_approved', 0),
            'trades_executed': trade_validation.get('trades_executed', 0),
            'kill_switch_status': 'clear' if not trading_blocked else 'triggered',
            'validation_gate_status': trade_validation.get('execution_gate', {}).get('status', 'unknown'),
            'next_action': next_action,
        },
        'account_check': account,
        'trade_validation': trade_validation,
        'orders_sent': 0,
    }
    if market_scan is not None:
        summary['market_scan'] = market_scan
    return summary


def write_cycle_outputs(summary, timestamp):
    REPORTS.mkdir(parents=True, exist_ok=True)
    entry = {
        'agent': 'Memory / Journal Agent',
        'event_type': 'daily_review',
        'symbol': 'N/A',
        'timestamp': timestamp.strftime('%Y-%m-%d %H:%M:%S'),
        'summary': 'Cycle sans ordre; trading bloqué tant que les prérequis ne sont pas complets.' if summary['account_check'].get('trading_blocked', True) else 'Compte vérifié; prêt pour scan prudent, aucun ordre automatique ici.',
        'data': summary,
    }
    with JOURNAL.open('a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    append_trade_validation_journal(JOURNAL, summary['trade_validation'], timestamp=timestamp.strftime('%Y-%m-%d %H:%M:%S'))
    report = REPORTS / f"cycle_{timestamp.strftime('%Y%m%d_%H%M%S')}.md"
    report.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return report


def renew_stop_losses(policy):
    """Re-place DAY stop orders for fractional positions (Alpaca requires DAY for fractional)."""
    import os
    base_url = os.environ.get('APCA_API_BASE_URL', 'https://paper-api.alpaca.markets')
    key_id = os.environ.get('APCA_API_KEY_ID', '')
    secret_key = os.environ.get('APCA_API_SECRET_KEY', '')
    if not key_id or not secret_key:
        return {'renewed': 0, 'reason': 'no_credentials'}

    headers = ['--header', f'APCA-API-KEY-ID: {key_id}', '--header', f'APCA-API-SECRET-KEY: {secret_key}']

    # Get positions
    r = subprocess.run(['curl', '-s'] + headers + [f'{base_url}/v2/positions'], capture_output=True, text=True, timeout=15)
    positions = json.loads(r.stdout or '[]')
    if not positions or isinstance(positions, dict):
        return {'renewed': 0, 'reason': 'no_positions'}

    # Get open orders
    r2 = subprocess.run(['curl', '-s'] + headers + [f'{base_url}/v2/orders?status=open'], capture_output=True, text=True, timeout=15)
    open_orders = json.loads(r2.stdout or '[]')

    # Find positions without active stop orders
    symbols_with_stop = set()
    for o in open_orders:
        if o.get('type') == 'stop' and o.get('side') == 'sell':
            symbols_with_stop.add(o.get('symbol'))

    risk_pct = policy.get('risk_mode', {}).get('max_risk_per_trade_pct', 1.0)
    portfolio_value = sum(float(p.get('market_value', 0)) for p in positions)
    # Add cash estimate
    r3 = subprocess.run(['curl', '-s'] + headers + [f'{base_url}/v2/account'], capture_output=True, text=True, timeout=15)
    acct = json.loads(r3.stdout or '{}')
    portfolio_value = float(acct.get('portfolio_value', portfolio_value))
    max_loss_per_trade = portfolio_value * risk_pct / 100.0

    renewed = 0
    for pos in positions:
        symbol = pos.get('symbol')
        if symbol in symbols_with_stop:
            continue
        qty = float(pos.get('qty', 0))
        entry = float(pos.get('avg_entry_price', 0))
        if qty <= 0 or entry <= 0:
            continue

        # Calculate stop: max_loss / qty below entry
        stop_distance = max_loss_per_trade / qty
        stop_price = round(entry - stop_distance, 2)
        if stop_price <= 0:
            continue

        # Fractional -> DAY, whole -> GTC
        is_fractional = (qty != int(qty))
        tif = 'day' if is_fractional else 'gtc'

        order = {
            'symbol': symbol,
            'qty': str(qty),
            'side': 'sell',
            'type': 'stop',
            'stop_price': str(stop_price),
            'time_in_force': tif
        }
        r4 = subprocess.run(
            ['curl', '-s', '-X', 'POST'] + headers +
            ['--header', 'Content-Type: application/json', '-d', json.dumps(order), f'{base_url}/v2/orders'],
            capture_output=True, text=True, timeout=15
        )
        resp = json.loads(r4.stdout or '{}')
        if resp.get('status') in ('accepted', 'new', 'pending_new'):
            renewed += 1

    return {'renewed': renewed, 'total_positions': len(positions)}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _fetch_closed_trades_from_alpaca() -> list:
    """Fetch closed orders (filled sells) from Alpaca to compute real P&L per trade."""
    import os
    base_url = os.environ.get('APCA_API_BASE_URL', 'https://paper-api.alpaca.markets')
    key_id = os.environ.get('APCA_API_KEY_ID', '')
    secret_key = os.environ.get('APCA_API_SECRET_KEY', '')
    if not key_id or not secret_key:
        return []

    headers = ['--header', f'APCA-API-KEY-ID: {key_id}', '--header', f'APCA-API-SECRET-KEY: {secret_key}']

    # Fetch account activities (trades) — FILL type gives us executed trades
    r = subprocess.run(
        ['curl', '-s'] + headers + [f'{base_url}/v2/account/activities/FILL?direction=desc&page_size=200'],
        capture_output=True, text=True, timeout=20
    )
    try:
        activities = json.loads(r.stdout or '[]')
    except Exception:
        activities = []

    if not isinstance(activities, list):
        return []
    return activities


def _build_setup_map_from_journal() -> Dict[str, Dict[str, Any]]:
    """Build a map: symbol -> {setup, entry, stop_loss, take_profit, timestamp} from journal trade executions."""
    trade_map: Dict[str, list] = {}
    try:
        with JOURNAL.open(encoding='utf-8') as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except Exception:
                    continue
                # Look for trade_execution or trade_validation_proposals entries
                etype = event.get('event_type', '')
                if etype == 'trade_execution':
                    data = event.get('data', {}) if isinstance(event.get('data'), dict) else {}
                    symbol = str(data.get('symbol', event.get('symbol', ''))).strip()
                    setup = str(data.get('setup', data.get('strategy', ''))).strip()
                    if symbol and setup:
                        trade_map.setdefault(symbol, []).append({
                            'setup': setup,
                            'entry': float(data.get('entry', data.get('limit_price', 0)) or 0),
                            'stop_loss': float(data.get('stop_loss', 0) or 0),
                            'take_profit': float(data.get('take_profit', 0) or 0),
                            'timestamp': event.get('timestamp', ''),
                        })
                elif etype == 'trade_validation_proposals':
                    data = event.get('data', {}) if isinstance(event.get('data'), dict) else {}
                    outputs = data.get('agent_outputs', {}) if isinstance(data.get('agent_outputs'), dict) else {}
                    tech_evals = outputs.get('Technical Analyst Agent', {}).get('evaluations', []) if isinstance(outputs.get('Technical Analyst Agent'), dict) else []
                    for item in tech_evals:
                        if not isinstance(item, dict):
                            continue
                        symbol = str(item.get('symbol', '')).strip()
                        setup = str(item.get('setup', '')).strip()
                        if symbol and setup and setup != 'no_clear_setup':
                            trade_map.setdefault(symbol, []).append({
                                'setup': setup,
                                'entry': float(item.get('entry', 0) or 0),
                                'stop_loss': float(item.get('stop_loss', 0) or 0),
                                'take_profit': float(item.get('take_profit', 0) or 0),
                                'timestamp': event.get('timestamp', ''),
                            })
    except FileNotFoundError:
        pass
    return trade_map


def _compute_setup_rotation_from_journal(policy: Dict[str, Any]) -> Dict[str, Any]:
    """V2: Build rolling expectancy by setup from REAL closed P&L (Alpaca fills)."""
    min_samples = 3  # Lower threshold for 100 USD account with few trades

    # 1. Fetch real fills from Alpaca
    activities = _fetch_closed_trades_from_alpaca()
    if not activities:
        return {'updated': False, 'reason': 'no_closed_trades_from_alpaca'}

    # 2. Build setup map from journal
    setup_map = _build_setup_map_from_journal()

    # 3. Reconstruct closed round-trips: BUY then SELL for same symbol
    # Group fills by symbol and side
    buys: Dict[str, list] = {}
    sells: Dict[str, list] = {}
    for act in activities:
        if not isinstance(act, dict):
            continue
        symbol = str(act.get('symbol', '')).strip()
        side = str(act.get('side', '')).lower()
        price = float(act.get('price', 0) or 0)
        qty = float(act.get('qty', 0) or 0)
        ts = str(act.get('transaction_time', act.get('timestamp', '')))
        if not symbol or price <= 0 or qty <= 0:
            continue
        entry_data = {'symbol': symbol, 'price': price, 'qty': qty, 'ts': ts}
        if side in ('buy', 'buy_to_cover'):
            buys.setdefault(symbol, []).append(entry_data)
        elif side in ('sell', 'sell_short'):
            sells.setdefault(symbol, []).append(entry_data)

    # 4. Match sells to buys (FIFO) and compute P&L per round-trip
    closed_trades: list = []
    for symbol, sell_list in sells.items():
        buy_list = buys.get(symbol, [])
        if not buy_list:
            continue
        # Sort both by timestamp
        buy_list.sort(key=lambda x: x['ts'])
        sell_list.sort(key=lambda x: x['ts'])
        buy_idx = 0
        for sell in sell_list:
            if buy_idx >= len(buy_list):
                break
            buy = buy_list[buy_idx]
            # Simple FIFO match
            pnl_per_share = sell['price'] - buy['price']
            matched_qty = min(sell['qty'], buy['qty'])
            pnl = pnl_per_share * matched_qty
            cost_basis = buy['price'] * matched_qty
            pnl_pct = (pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0
            closed_trades.append({
                'symbol': symbol,
                'entry_price': buy['price'],
                'exit_price': sell['price'],
                'qty': matched_qty,
                'pnl': round(pnl, 4),
                'pnl_pct': round(pnl_pct, 3),
                'buy_ts': buy['ts'],
                'sell_ts': sell['ts'],
            })
            buy_idx += 1

    if not closed_trades:
        return {'updated': False, 'reason': 'no_completed_round_trips'}

    # 5. Map each closed trade to its setup via journal
    stats: Dict[str, Dict[str, float]] = {}
    unmatched = 0
    for trade in closed_trades:
        symbol = trade['symbol']
        journal_entries = setup_map.get(symbol, [])
        # Find the most recent journal entry BEFORE this trade's buy timestamp
        setup = 'unknown'
        entry_price_journal = 0.0
        for je in reversed(journal_entries):
            if je['timestamp'] <= trade['buy_ts'] or not je['timestamp']:
                setup = je['setup']
                entry_price_journal = je['entry']
                break
        if not journal_entries:
            # Fallback: if we have no journal entry, try first available
            setup = 'unknown'
            unmatched += 1

        if setup == 'unknown' or setup == 'no_clear_setup':
            unmatched += 1
            setup = 'untracked'

        # Compute risk-reward realized
        stop_distance = 0.0
        for je in reversed(journal_entries):
            if je.get('stop_loss', 0) > 0 and je.get('entry', 0) > 0:
                stop_distance = abs(je['entry'] - je['stop_loss'])
                break
        realized_rr = (trade['pnl'] / (stop_distance * trade['qty'])) if stop_distance > 0 and trade['qty'] > 0 else trade['pnl_pct'] / 1.5

        rec = stats.setdefault(setup, {'samples': 0.0, 'wins': 0.0, 'total_pnl': 0.0, 'total_rr': 0.0})
        rec['samples'] += 1.0
        rec['wins'] += 1.0 if trade['pnl'] > 0 else 0.0
        rec['total_pnl'] += trade['pnl']
        rec['total_rr'] += realized_rr

    # 6. Compute expectancy per setup
    expectancy_rows = []
    disabled_setups: list[str] = []
    boosted_setups: list[str] = []

    for setup, rec in stats.items():
        samples = int(rec['samples'])
        if samples <= 0:
            continue
        win_rate = rec['wins'] / rec['samples']
        avg_pnl = rec['total_pnl'] / rec['samples']
        avg_rr = rec['total_rr'] / rec['samples']
        # Expectancy = average P&L per trade (real USD)
        expectancy_usd = avg_pnl
        # Normalized expectancy = (win_rate * avg_win_rr) - (loss_rate * 1.0)
        expectancy_norm = (win_rate * max(avg_rr, 0)) - (1.0 - win_rate)

        row = {
            'setup': setup,
            'samples': samples,
            'win_rate': round(win_rate, 3),
            'avg_pnl_usd': round(avg_pnl, 4),
            'avg_rr_realized': round(avg_rr, 3),
            'expectancy_usd': round(expectancy_usd, 4),
            'expectancy_normalized': round(expectancy_norm, 3),
            'total_pnl_usd': round(rec['total_pnl'], 4),
        }
        expectancy_rows.append(row)

        if samples >= min_samples and expectancy_norm < -0.1:
            disabled_setups.append(setup)
        elif samples >= min_samples and expectancy_norm >= 0.6:
            boosted_setups.append(setup)

    expectancy_rows.sort(key=lambda r: (r['expectancy_normalized'], r['samples']), reverse=True)

    rotation = {
        'enabled': True,
        'source': 'real_pnl_alpaca_v2',
        'min_samples': min_samples,
        'closed_trades_analyzed': len(closed_trades),
        'unmatched_to_setup': unmatched,
        'disabled_setups': sorted(disabled_setups),
        'boosted_setups': sorted(boosted_setups),
        'stats': expectancy_rows[:12],
        'updated_at': dt.datetime.now().isoformat(),
    }
    policy['setup_rotation'] = rotation
    save_policy(policy)
    return {'updated': True, 'rotation': rotation}


def adapt_policy_if_due(policy: Dict[str, Any], account: Dict[str, Any], now: dt.datetime) -> Dict[str, Any]:
    meta = policy.get('adaptive_meta', {}) if isinstance(policy.get('adaptive_meta'), dict) else {}
    last_run = str(meta.get('last_adaptation_ts', '')).strip()
    if last_run:
        try:
            last_dt = dt.datetime.fromisoformat(last_run)
            if now - last_dt < dt.timedelta(hours=24):
                return {'applied': False, 'reason': 'cooldown_24h'}
        except Exception:
            pass

    equity = float(account.get('portfolio_value', 0) or 0)
    if equity <= 0:
        return {'applied': False, 'reason': 'no_equity_data'}

    start_equity = float(meta.get('start_equity', equity) or equity)
    high_water_mark = float(meta.get('high_water_mark', equity) or equity)
    high_water_mark = max(high_water_mark, equity)
    drawdown_pct = ((equity - high_water_mark) / high_water_mark * 100.0) if high_water_mark > 0 else 0.0

    target_return = float(policy.get('target_return_pct', 10) or 10)
    realized_return = ((equity - start_equity) / start_equity * 100.0) if start_equity > 0 else 0.0
    progress_gap = target_return - realized_return

    rm = policy.get('risk_mode', {}) if isinstance(policy.get('risk_mode'), dict) else {}
    risk = float(rm.get('max_risk_per_trade_pct', policy.get('max_risk_per_trade_pct', 1.0)) or 1.0)
    expo = float(rm.get('max_total_exposure_pct', policy.get('max_total_exposure_pct', 50)) or 50)
    conf = float(rm.get('min_confidence', policy.get('min_confidence', 7.0)) or 7.0)
    rr = float(rm.get('min_risk_reward', policy.get('min_risk_reward', 2.0)) or 2.0)

    action = 'hold'
    if drawdown_pct <= -3.0:
        risk -= 0.3
        expo -= 8
        conf += 0.5
        rr += 0.1
        action = 'derisk_drawdown'
    elif progress_gap > 4.0 and drawdown_pct > -1.5:
        risk += 0.2
        expo += 5
        conf -= 0.3
        rr -= 0.05
        action = 'increase_aggression_behind_target'
    elif progress_gap < -2.0:
        risk -= 0.1
        expo -= 3
        conf += 0.2
        rr += 0.05
        action = 'lock_gains_ahead_target'

    risk = round(_clamp(risk, 0.5, 2.5), 2)
    expo = round(_clamp(expo, 30.0, 85.0), 1)
    conf = round(_clamp(conf, 4.5, 8.5), 2)
    rr = round(_clamp(rr, 1.3, 2.5), 2)

    rm['max_risk_per_trade_pct'] = risk
    rm['max_total_exposure_pct'] = expo
    rm['min_confidence'] = conf
    rm['min_risk_reward'] = rr
    policy['risk_mode'] = rm
    policy['max_risk_per_trade_pct'] = risk
    policy['max_total_exposure_pct'] = expo
    policy['min_confidence'] = conf
    policy['min_risk_reward'] = rr

    policy['adaptive_meta'] = {
        'last_adaptation_ts': now.isoformat(),
        'start_equity': start_equity,
        'high_water_mark': high_water_mark,
        'realized_return_pct': round(realized_return, 2),
        'drawdown_pct': round(drawdown_pct, 2),
        'progress_gap_pct': round(progress_gap, 2),
        'action': action,
    }
    save_policy(policy)
    return {
        'applied': True,
        'action': action,
        'realized_return_pct': round(realized_return, 2),
        'drawdown_pct': round(drawdown_pct, 2),
        'progress_gap_pct': round(progress_gap, 2),
        'new_limits': {
            'max_risk_per_trade_pct': risk,
            'max_total_exposure_pct': expo,
            'min_confidence': conf,
            'min_risk_reward': rr,
        },
    }


def main():
    now = dt.datetime.now()
    code, account = run_check()
    market_scan = None
    if code == 0 and not account.get('trading_blocked', True):
        # Read-only scan: assets, bars, and quotes only. run_live_scan never submits orders.
        market_scan = run_live_scan()
    policy = load_policy()

    adaptation = adapt_policy_if_due(policy, account, now)
    if adaptation.get('applied'):
        policy = load_policy()

    setup_rotation = _compute_setup_rotation_from_journal(policy)
    if setup_rotation.get('updated'):
        policy = load_policy()

    # Renew stop losses for positions without active stops
    stop_renewal = renew_stop_losses(policy)
    summary = build_summary(account, policy=policy, market_scan=market_scan)
    summary['stop_renewal'] = stop_renewal
    summary['adaptive_update'] = adaptation
    summary['setup_rotation_update'] = setup_rotation

    # AUTO-EXECUTION: if proposals exist and execution is authorized, submit orders
    proposals = summary.get('trade_validation', {}).get('proposals', [])
    execution_auth = policy.get('execution_authorization', {})
    auto_execute = (
        execution_auth.get('alpaca_paper_orders_after_full_pipeline', False)
        and execution_auth.get('authorized_by_user', False)
        and not account.get('trading_blocked', False)
        and proposals
    )
    if auto_execute:
        exec_result = execute_proposals(proposals, policy)
        summary['execution'] = exec_result
        summary['orders_sent'] = exec_result.get('orders_sent', 0)
    else:
        summary['execution'] = {'orders_sent': 0, 'reason': 'no_proposals_or_not_authorized'}

    write_cycle_outputs(summary, now)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if code == 0 else code


if __name__ == '__main__':
    sys.exit(main())
