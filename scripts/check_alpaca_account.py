#!/usr/bin/env python3
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_KEY_ENV = ("APCA_API_KEY_ID", "ALPACA_API_KEY")
SECRET_ENV = ("APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY")
DEFAULT_BASE_URL = "https://paper-api.alpaca.markets"


def _first_env(names):
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _base_result(base_url, api_key=None, secret=None):
    return {
        "agent": "Cash Control Agent",
        "broker": "Alpaca Paper",
        "paper_endpoint": base_url,
        "credentials_present": bool(api_key and secret),
        "account_verified": False,
        "trading_blocked": True,
        "reason": "missing_credentials",
    }


def _safe_float(value, default=0.0):
    try:
        parsed = float(value if value is not None else default)
    except (TypeError, ValueError):
        return float(default)
    return parsed if math.isfinite(parsed) else float(default)


def _required_float(account, field):
    value = account.get(field)
    if value is None:
        return None, f"invalid_account_{field}"
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None, f"invalid_account_{field}"
    if not math.isfinite(parsed):
        return None, f"invalid_account_{field}"
    return parsed, None


def _is_paper_base_url(base_url):
    parsed = urllib.parse.urlparse(base_url)
    return parsed.scheme == "https" and parsed.hostname == "paper-api.alpaca.markets"


def _get_json(base_url, path, api_key, secret, urlopen=urllib.request.urlopen):
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret},
    )
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _compact_position(position):
    return {
        "symbol": position.get("symbol"),
        "qty": position.get("qty"),
        "side": position.get("side"),
        "market_value": position.get("market_value"),
        "avg_entry_price": position.get("avg_entry_price"),
        "current_price": position.get("current_price"),
        "unrealized_pl": position.get("unrealized_pl"),
        "unrealized_plpc": position.get("unrealized_plpc"),
    }


def _compact_order(order):
    return {
        "id": order.get("id"),
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "qty": order.get("qty"),
        "status": order.get("status"),
        "type": order.get("type"),
        "limit_price": order.get("limit_price"),
        "stop_price": order.get("stop_price"),
        "submitted_at": order.get("submitted_at"),
    }


def _is_list_of_dicts(payload):
    return isinstance(payload, list) and all(isinstance(item, dict) for item in payload)


def _incoherence_reason(account):
    cash, cash_error = _required_float(account, "cash")
    if cash_error:
        return cash_error
    buying_power, buying_power_error = _required_float(account, "buying_power")
    if buying_power_error:
        return buying_power_error
    equity, equity_error = _required_float(account, "equity")
    if equity_error:
        return equity_error
    multiplier, multiplier_error = _required_float(account, "multiplier")
    if multiplier_error:
        return multiplier_error

    if account.get("trading_blocked") or account.get("account_blocked"):
        return "broker_or_account_blocked"
    if cash < 0:
        return "negative_cash"
    if equity < 0:
        return "negative_equity"
    if buying_power < 0:
        return "negative_buying_power"
    if multiplier != 1.0 or buying_power > cash + 0.01:
        return "cash_buying_power_margin_incoherence"
    return None


def check_alpaca_account(api_key=None, secret=None, base_url=None, urlopen=urllib.request.urlopen):
    api_key = api_key if api_key is not None else _first_env(API_KEY_ENV)
    secret = secret if secret is not None else _first_env(SECRET_ENV)
    base_url = base_url or os.getenv("APCA_API_BASE_URL", DEFAULT_BASE_URL)
    result = _base_result(base_url, api_key, secret)

    if not api_key or not secret:
        return result

    if not _is_paper_base_url(base_url):
        result["reason"] = "base_url_not_paper"
        return result

    try:
        account = _get_json(base_url, "/v2/account", api_key, secret, urlopen=urlopen)
        positions_raw = _get_json(base_url, "/v2/positions", api_key, secret, urlopen=urlopen)
        orders_raw = _get_json(base_url, "/v2/orders?status=open", api_key, secret, urlopen=urlopen)
    except urllib.error.HTTPError as e:
        result["reason"] = f"http_error_{e.code}"
        return result
    except Exception as e:
        result["reason"] = type(e).__name__
        return result

    if not isinstance(account, dict):
        result["reason"] = "invalid_account_payload"
        return result
    if not _is_list_of_dicts(positions_raw):
        result.update({"account_verified": True, "reason": "invalid_positions_payload"})
        return result
    if not _is_list_of_dicts(orders_raw):
        result.update({"account_verified": True, "reason": "invalid_orders_payload"})
        return result

    cash = _safe_float(account.get("cash"))
    equity = _safe_float(account.get("equity"))
    buying_power = _safe_float(account.get("buying_power"))
    multiplier = _safe_float(account.get("multiplier"), 1.0)
    positions = [_compact_position(p) for p in positions_raw]
    open_orders = [_compact_order(o) for o in orders_raw]
    incoherence = _incoherence_reason(account)

    result.update({
        "account_verified": True,
        "trading_blocked": bool(incoherence),
        "reason": incoherence or "ok",
        "cash": round(cash, 2),
        "buying_power": round(buying_power, 2),
        "portfolio_value": round(equity, 2),
        "margin_multiplier": multiplier,
        "margin_available_ignored": bool(multiplier != 1.0 or buying_power > cash + 0.01),
        "cash_control_basis": "cash_only",
        "pattern_day_trader": account.get("pattern_day_trader"),
        "trading_blocked_by_broker": account.get("trading_blocked"),
        "account_blocked": account.get("account_blocked"),
        "open_positions_count": len(positions),
        "open_orders_count": len(open_orders),
        "positions": positions,
        "open_orders": open_orders,
    })
    return result


def exit_code_for_result(result):
    reason = result.get("reason")
    if reason == "ok":
        return 0
    if reason == "missing_credentials":
        return 2
    if reason == "base_url_not_paper":
        return 3
    if str(reason).startswith("http_error_"):
        return 4
    if result.get("account_verified") and result.get("trading_blocked"):
        return 6
    return 5


def main():
    result = check_alpaca_account()
    print(json.dumps(result, ensure_ascii=False))
    return exit_code_for_result(result)


if __name__ == "__main__":
    sys.exit(main())
