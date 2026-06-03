#!/usr/bin/env python3
"""Historical setup backtester and walk-forward policy recommender."""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

try:
    from .market_scanner import analyze_technicals, finite_float
except ImportError:
    from market_scanner import analyze_technicals, finite_float


def _trade_outcome(entry: float, stop: float, target: float, future_bars: list[Mapping[str, Any]], max_hold_bars: int = 10) -> dict[str, Any]:
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


def backtest_setup(setup: str, bars_by_symbol: Mapping[str, list[Mapping[str, Any]]], *, min_history: int = 60, max_hold_bars: int = 10, min_trades: int = 8) -> dict[str, Any]:
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


def walk_forward_backtest(bars_by_symbol: Mapping[str, list[Mapping[str, Any]]], *, setups: Iterable[str], window: int = 80, step: int = 20) -> dict[str, Any]:
    max_len = max((len(bars) for bars in bars_by_symbol.values()), default=0)
    folds: list[dict[str, Any]] = []
    if max_len < window + step:
        return {"folds": 0, "results": []}
    start = 0
    while start + window + step <= max_len:
        train_slice = {sym: bars[start : start + window] for sym, bars in bars_by_symbol.items() if len(bars) >= start + window}
        test_slice = {sym: bars[start + window - 60 : start + window + step] for sym, bars in bars_by_symbol.items() if len(bars) >= start + window + step}
        setup_results = [backtest_setup(setup, test_slice, min_trades=1) for setup in setups]
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
