#!/usr/bin/env python3
"""Historical setup backtester and walk-forward policy recommender."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import urllib.parse
import urllib.request
from typing import Any, Iterable, Mapping, Sequence

try:
    from .market_scanner import analyze_technicals, finite_float
except ImportError:
    from market_scanner import analyze_technicals, finite_float


def _trade_outcome(entry: float, stop: float, target: float, future_bars: Sequence[Mapping[str, Any]], max_hold_bars: int = 10) -> dict[str, Any]:
    if entry <= 0 or stop <= 0 or stop >= entry or target <= entry:
        return {"valid": False}
    risk = entry - stop
    inspected = future_bars[:max_hold_bars]
    for idx, bar in enumerate(inspected, start=1):
        low = finite_float(bar.get("l") or bar.get("low"))
        high = finite_float(bar.get("h") or bar.get("high"))
        if low <= stop:
            return {"valid": True, "r": -1.0, "exit_reason": "stop", "hold_bars": idx}
        if high >= target:
            return {"valid": True, "r": round((target - entry) / risk, 3), "exit_reason": "target", "hold_bars": idx}
    if not inspected:
        return {"valid": False}
    close = finite_float(inspected[-1].get("c") or inspected[-1].get("close"))
    return {"valid": True, "r": round((close - entry) / risk, 3), "exit_reason": "time_stop", "hold_bars": len(inspected)}


def _summarize_trades(setup: str, trades: list[dict[str, Any]], min_trades: int) -> dict[str, Any]:
    if not trades:
        return {"setup": setup, "trades": 0, "win_rate": 0.0, "avg_r": 0.0, "max_drawdown_r": 0.0, "sample_ok": False, "trades_detail": []}
    r_values = [float(t["r"]) for t in trades]
    wins = sum(1 for value in r_values if value > 0)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in r_values:
        equity += value
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return {
        "setup": setup,
        "trades": len(trades),
        "win_rate": round(wins / len(trades), 3),
        "avg_r": round(sum(r_values) / len(r_values), 3),
        "max_drawdown_r": round(max_dd, 3),
        "avg_hold_bars": round(sum(float(t["hold_bars"]) for t in trades) / len(trades), 2),
        "target_hits": sum(1 for t in trades if t.get("exit_reason") == "target"),
        "stop_hits": sum(1 for t in trades if t.get("exit_reason") == "stop"),
        "time_stops": sum(1 for t in trades if t.get("exit_reason") == "time_stop"),
        "sample_ok": len(trades) >= min_trades,
        "trades_detail": trades[:100],
    }


def backtest_setup(setup: str, bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]], *, min_history: int = 60, max_hold_bars: int = 10, min_trades: int = 8) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    for symbol, raw_bars in bars_by_symbol.items():
        bars = [dict(bar) for bar in raw_bars]
        if len(bars) < min_history + max_hold_bars + 1:
            continue
        asset_type = "etf" if symbol.upper() in {"SPY", "QQQ", "IWM", "DIA"} else "stock"
        for idx in range(min_history, len(bars) - max_hold_bars):
            window = bars[:idx]
            analysis = analyze_technicals(symbol.upper(), asset_type, window)
            if analysis.get("setup") != setup:
                continue
            outcome = _trade_outcome(float(analysis["entry"]), float(analysis["stop_loss"]), float(analysis["take_profit"]), bars[idx : idx + max_hold_bars], max_hold_bars)
            if outcome.get("valid"):
                trades.append({"symbol": symbol.upper(), "setup": setup, "entry_index": idx, **outcome})
    return _summarize_trades(setup, trades, min_trades)


def backtest_setups_fast(setups: Iterable[str], bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]], *, min_history: int = 60, max_hold_bars: int = 10, min_trades: int = 8) -> list[dict[str, Any]]:
    setup_list = list(setups)
    wanted = set(setup_list)
    trades_by_setup: dict[str, list[dict[str, Any]]] = {setup: [] for setup in setup_list}
    for symbol, raw_bars in bars_by_symbol.items():
        bars = [dict(bar) for bar in raw_bars]
        if len(bars) < min_history + max_hold_bars + 1:
            continue
        asset_type = "etf" if symbol.upper() in {"SPY", "QQQ", "IWM", "DIA"} else "stock"
        for idx in range(min_history, len(bars) - max_hold_bars):
            analysis = analyze_technicals(symbol.upper(), asset_type, bars[:idx])
            setup = str(analysis.get("setup") or "")
            if setup not in wanted:
                continue
            outcome = _trade_outcome(float(analysis["entry"]), float(analysis["stop_loss"]), float(analysis["take_profit"]), bars[idx : idx + max_hold_bars], max_hold_bars)
            if outcome.get("valid"):
                trades_by_setup[setup].append({"symbol": symbol.upper(), "setup": setup, "entry_index": idx, **outcome})
    return [_summarize_trades(setup, trades_by_setup[setup], min_trades) for setup in setup_list]


def walk_forward_backtest(bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]], *, setups: Iterable[str], window: int = 80, step: int = 20) -> dict[str, Any]:
    max_len = max((len(bars) for bars in bars_by_symbol.values()), default=0)
    folds: list[dict[str, Any]] = []
    if max_len < window + step:
        return {"folds": 0, "results": []}
    start = 0
    while start + window + step <= max_len:
        test_slice = {sym: bars[start + window - 60 : start + window + step] for sym, bars in bars_by_symbol.items() if len(bars) >= start + window + step}
        setup_results = backtest_setups_fast(setups, test_slice, min_trades=1)
        folds.append({"train_start": start, "test_start": start + window, "test_end": start + window + step, "results": setup_results})
        start += step
    return {"folds": len(folds), "results": folds}


def recommend_setup_rotation(results: Iterable[Mapping[str, Any]], *, min_samples: int = 20, boost_avg_r: float = 0.25, disable_avg_r: float = -0.15) -> dict[str, Any]:
    boosted: list[str] = []
    disabled: list[str] = []
    stats: list[dict[str, Any]] = []
    for row in results:
        setup = str(row.get("setup", ""))
        trades = int(row.get("trades", 0) or 0)
        avg_r = float(row.get("avg_r", 0) or 0)
        stats.append({"setup": setup, "trades": trades, "win_rate": row.get("win_rate", 0), "avg_r": round(avg_r, 3), "max_drawdown_r": row.get("max_drawdown_r", 0)})
        if trades >= min_samples and avg_r >= boost_avg_r:
            boosted.append(setup)
        if trades >= min_samples and avg_r <= disable_avg_r:
            disabled.append(setup)
    stats.sort(key=lambda item: (item["avg_r"], item["trades"]), reverse=True)
    return {"enabled": True, "source": "historical_walk_forward_v1", "min_samples": min_samples, "boosted_setups": sorted(boosted), "disabled_setups": sorted(disabled), "stats": stats}


DEFAULT_SETUPS = ["momentum_continuation", "pullback_ema21", "breakout_volume", "support_bounce", "etf_trend_following"]
DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "META", "AMZN", "TSLA", "AVGO", "SMCI", "PLTR", "COIN", "HOOD", "SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV"]


def _to_epoch(date_text: str) -> int:
    parsed = dt.datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp())


def fetch_yahoo_daily_bars(symbol: str, start: str, end: str, *, timeout: int = 20) -> list[dict[str, Any]]:
    """Fetch split-adjusted daily bars from Yahoo chart API; no trading credentials required."""
    period1 = _to_epoch(start)
    period2 = _to_epoch(end) + 86400
    query = urllib.parse.urlencode({"period1": period1, "period2": period2, "interval": "1d", "events": "history", "includeAdjustedClose": "true"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol.upper())}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        return []
    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote") or [None])[0]) or {}
    adjusted = (((result.get("indicators") or {}).get("adjclose") or [None])[0]) or {}
    adjclose = adjusted.get("adjclose") or []
    bars: list[dict[str, Any]] = []
    for i, ts in enumerate(timestamps):
        try:
            close = quote.get("close", [])[i]
            adj = adjclose[i] if i < len(adjclose) else close
            if not close or not adj:
                continue
            factor = float(adj) / float(close)
            bar = {
                "t": dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).date().isoformat(),
                "o": round(float(quote.get("open", [])[i]) * factor, 6),
                "h": round(float(quote.get("high", [])[i]) * factor, 6),
                "l": round(float(quote.get("low", [])[i]) * factor, 6),
                "c": round(float(adj), 6),
                "v": int(quote.get("volume", [])[i] or 0),
            }
        except (IndexError, TypeError, ValueError, OverflowError):
            continue
        if all(math.isfinite(float(bar[key])) for key in ("o", "h", "l", "c")) and bar["v"] >= 0:
            bars.append(bar)
    return bars


def load_bars_from_json(path: pathlib.Path) -> dict[str, list[dict[str, Any]]]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("bars JSON must be an object: {SYMBOL: [bars...]}")
    return {str(symbol).upper(): [dict(bar) for bar in bars if isinstance(bar, dict)] for symbol, bars in data.items() if isinstance(bars, list)}


def fetch_bars(symbols: Iterable[str], start: str, end: str) -> dict[str, list[dict[str, Any]]]:
    bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        cleaned = symbol.strip().upper()
        if not cleaned:
            continue
        try:
            bars = fetch_yahoo_daily_bars(cleaned, start, end)
        except Exception as exc:  # network failures should not abort the whole batch
            bars_by_symbol[cleaned] = []
            print(f"WARN {cleaned}: {type(exc).__name__}: {exc}")
            continue
        bars_by_symbol[cleaned] = bars
    return bars_by_symbol


def run_backtest(bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]], *, setups: Iterable[str] = DEFAULT_SETUPS, min_trades: int = 8, max_hold_bars: int = 10) -> dict[str, Any]:
    setup_results = backtest_setups_fast(setups, bars_by_symbol, min_trades=min_trades, max_hold_bars=max_hold_bars)
    walk = walk_forward_backtest(bars_by_symbol, setups=setups)
    recommendation = recommend_setup_rotation(setup_results, min_samples=min_trades)
    symbols_loaded = {symbol: len(bars) for symbol, bars in bars_by_symbol.items()}
    return {"symbols_loaded": symbols_loaded, "setups": setup_results, "walk_forward": walk, "recommendation": recommendation}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Monseignor historical setup backtests.")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="Comma-separated symbols to fetch; ignored with --bars-json")
    parser.add_argument("--start", default=(dt.date.today() - dt.timedelta(days=365 * 3)).isoformat(), help="YYYY-MM-DD start date")
    parser.add_argument("--end", default=dt.date.today().isoformat(), help="YYYY-MM-DD end date")
    parser.add_argument("--setups", default=",".join(DEFAULT_SETUPS), help="Comma-separated setup names")
    parser.add_argument("--bars-json", type=pathlib.Path, help="Optional local {SYMBOL: bars[]} JSON instead of Yahoo fetch")
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("reports/backtests/latest_backtest.json"), help="Output JSON path")
    parser.add_argument("--min-trades", type=int, default=8)
    parser.add_argument("--max-hold-bars", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    setups = [item.strip() for item in args.setups.split(",") if item.strip()]
    if args.bars_json:
        bars_by_symbol = load_bars_from_json(args.bars_json)
    else:
        symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
        bars_by_symbol = fetch_bars(symbols, args.start, args.end)
    result = run_backtest(bars_by_symbol, setups=setups, min_trades=args.min_trades, max_hold_bars=args.max_hold_bars)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(json.dumps({"out": str(args.out), "symbols": len(result["symbols_loaded"]), "setups": result["recommendation"]["stats"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
