#!/usr/bin/env python3
"""Prudent Alpaca market scanner.

Scans US equities (including allowed ETFs traded as us_equity assets), filters for
high liquidity and tight spreads, and emits JSON only. This module never submits
orders; it only calls Alpaca account/assets/market-data read endpoints.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any, Iterable

BASE = pathlib.Path(__file__).resolve().parents[1]
POLICY_PATH = BASE / "config" / "policy.json"
JOURNAL = BASE / "journal" / "events.jsonl"
REPORTS = BASE / "reports"

APPROVED_LIQUID_ETFS = {
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "VTI",
    "VOO",
    "IVV",
    "VEA",
    "VWO",
    "EFA",
    "EEM",
    "TLT",
    "IEF",
    "SHY",
    "HYG",
    "LQD",
    "GLD",
    "SLV",
    "XLK",
    "XLF",
    "XLE",
    "XLV",
    "XLY",
    "XLI",
    "XLP",
    "XLU",
    "XLB",
    "XLRE",
    "XLC",
}
MAJOR_US_EXCHANGES = {"NASDAQ", "NYSE", "ARCA", "AMEX", "BATS", "IEX"}
ETF_NAME_MARKERS = (
    " ETF",
    "EXCHANGE TRADED FUND",
    "PROSHARES",
    "DIREXION",
    "ISHARES",
    "SPDR",
    "VANGUARD ETF",
    "INVESCO QQQ",
    "WISDOMTREE",
)


@dataclasses.dataclass(frozen=True)
class MarketScannerConfig:
    min_price: float = 5.0
    max_price: float = 1_000.0
    min_volume: int = 1_000_000
    min_dollar_volume: float = 20_000_000.0
    max_spread_bps: float = 10.0
    top_n: int = 25
    max_universe: int = 500
    require_fractionable_over_cash: bool = True
    feed: str = "iex"

    @classmethod
    def from_policy(cls, policy: dict[str, Any]) -> "MarketScannerConfig":
        raw = policy.get("market_scanner", {}) if isinstance(policy, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        fields = {field.name for field in dataclasses.fields(cls)}
        values = {key: raw[key] for key in raw if key in fields}
        return cls(**values)


class AlpacaReadOnlyClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY")
        self.secret = os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
        self.trading_base_url = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
        self.data_base_url = os.getenv("APCA_DATA_BASE_URL", "https://data.alpaca.markets").rstrip("/")

    @property
    def credentials_present(self) -> bool:
        return bool(self.api_key and self.secret)

    def _headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self.api_key or "", "APCA-API-SECRET-KEY": self.secret or ""}

    def get_json(self, base_url: str, path: str, params: dict[str, Any] | None = None) -> Any:
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(base_url + path + query, headers=self._headers(), method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def account(self) -> dict[str, Any]:
        return self.get_json(self.trading_base_url, "/v2/account")

    def assets(self) -> list[dict[str, Any]]:
        data = self.get_json(self.trading_base_url, "/v2/assets", {"status": "active", "asset_class": "us_equity"})
        return data if isinstance(data, list) else []

    def latest_quotes(self, symbols: list[str], feed: str) -> dict[str, dict[str, Any]]:
        quotes: dict[str, dict[str, Any]] = {}
        for chunk in chunks(symbols, 100):
            data = self.get_json(self.data_base_url, "/v2/stocks/quotes/latest", {"symbols": ",".join(chunk), "feed": feed})
            quotes.update(data.get("quotes", {}) if isinstance(data, dict) else {})
        return quotes

    def latest_daily_bars(self, symbols: list[str], feed: str) -> dict[str, dict[str, Any]]:
        bars: dict[str, dict[str, Any]] = {}
        for chunk in chunks(symbols, 100):
            data = self.get_json(
                self.data_base_url,
                "/v2/stocks/bars",
                # Alpaca's `limit` is applied to the whole multi-symbol request, not per symbol.
                # Use chunk_size * 2 to maximize coverage (we only need 1 bar per symbol).
                {"symbols": ",".join(chunk), "timeframe": "1Day", "limit": len(chunk) * 2, "feed": feed},
            )
            raw_bars = data.get("bars", {}) if isinstance(data, dict) else {}
            for symbol, values in raw_bars.items():
                if isinstance(values, list) and values:
                    bars[symbol] = values[-1]
                elif isinstance(values, dict):
                    bars[symbol] = values
        return bars

    def historical_daily_bars(self, symbols: list[str], feed: str, limit: int = 120) -> dict[str, list[dict[str, Any]]]:
        bars: dict[str, list[dict[str, Any]]] = {}
        # Use the single-symbol endpoint: Alpaca's multi-symbol `limit` is global,
        # so a 7-symbol request with limit=120 may return ~17 bars per symbol.
        end = dt.datetime.now(dt.timezone.utc)
        start = end - dt.timedelta(days=max(limit * 3, 180))
        for symbol in symbols:
            data = self.get_json(
                self.data_base_url,
                f"/v2/stocks/{urllib.parse.quote(symbol)}/bars",
                {"timeframe": "1Day", "limit": limit, "feed": feed, "start": start.isoformat(), "end": end.isoformat(), "sort": "desc"},
            )
            values = data.get("bars", []) if isinstance(data, dict) else []
            cleaned = [bar for bar in values if isinstance(bar, dict)] if isinstance(values, list) else []
            if cleaned:
                bars[symbol] = list(reversed(cleaned[-limit:]))
        return bars


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def load_policy(path: pathlib.Path = POLICY_PATH) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def is_allowed_asset(asset: dict[str, Any]) -> bool:
    return (
        asset.get("status") == "active"
        and asset.get("tradable") is True
        and asset.get("class") == "us_equity"
        and asset.get("exchange") in MAJOR_US_EXCHANGES
        and bool(asset.get("symbol"))
    )


def classify_symbol(symbol: str) -> str:
    return "etf" if symbol.upper() in APPROVED_LIQUID_ETFS else "stock"


def looks_like_etf(asset: dict[str, Any]) -> bool:
    symbol = str(asset.get("symbol", "")).upper()
    if symbol in APPROVED_LIQUID_ETFS:
        return True
    name = str(asset.get("name") or asset.get("description") or "").upper()
    return any(marker in name for marker in ETF_NAME_MARKERS)


def asset_type(asset: dict[str, Any]) -> str:
    return "etf" if looks_like_etf(asset) else "stock"


def is_unapproved_etf(asset: dict[str, Any]) -> bool:
    return looks_like_etf(asset) and str(asset.get("symbol", "")).upper() not in APPROVED_LIQUID_ETFS


def quote_spread_bps(quote: dict[str, Any]) -> tuple[float, float, float]:
    bid = finite_float(quote.get("bp") or quote.get("bid_price"))
    ask = finite_float(quote.get("ap") or quote.get("ask_price"))
    if bid <= 0 or ask <= 0 or ask < bid:
        return bid, ask, float("inf")
    mid = (bid + ask) / 2
    return bid, ask, ((ask - bid) / mid) * 10_000



def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2 / (period + 1)
    ema = values[0]
    for value in values[1:]:
        ema = (value * k) + (ema * (1 - k))
    return ema


def _rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 0.0
    gains: list[float] = []
    losses: list[float] = []
    for prev, cur in zip(values[-period - 1 : -1], values[-period:]):
        change = cur - prev
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr(bars: list[dict[str, Any]], period: int = 14) -> float:
    if len(bars) <= period:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(bars)):
        high = finite_float(bars[i].get("h") or bars[i].get("high"))
        low = finite_float(bars[i].get("l") or bars[i].get("low"))
        prev_close = finite_float(bars[i - 1].get("c") or bars[i - 1].get("close"))
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs[-period:]) / period if len(trs) >= period else 0.0


def _macd(values: list[float]) -> tuple[float, float, float]:
    if len(values) < 35:
        return 0.0, 0.0, 0.0
    macd_series: list[float] = []
    for i in range(26, len(values) + 1):
        window = values[:i]
        macd_series.append(_ema(window, 12) - _ema(window, 26))
    macd_line = macd_series[-1]
    signal = _ema(macd_series, 9)
    return macd_line, signal, macd_line - signal


def analyze_technicals(symbol: str, asset_type: str, bars: list[dict[str, Any]], quote: dict[str, Any] | None = None) -> dict[str, Any]:
    if len(bars) < 60:
        return {"agent": "Technical Analyst Agent", "symbol": symbol, "direction": "long", "setup": "incomplete_data", "technical_score": 0, "confidence": 0, "risk_reward": 0, "technical_summary": "historique insuffisant"}
    closes = [finite_float(bar.get("c") or bar.get("close")) for bar in bars]
    highs = [finite_float(bar.get("h") or bar.get("high")) for bar in bars]
    lows = [finite_float(bar.get("l") or bar.get("low")) for bar in bars]
    volumes = [finite_float(bar.get("v") or bar.get("volume")) for bar in bars]
    close = closes[-1]
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    ema50 = _ema(closes, 50)
    rsi14 = _rsi(closes, 14)
    macd_line, macd_signal, macd_hist = _macd(closes)
    atr14 = _atr(bars, 14)
    support = min(lows[-20:])
    resistance = max(highs[-21:-1]) if len(highs) > 21 else max(highs[:-1])
    avg_vol20 = sum(volumes[-21:-1]) / min(20, len(volumes) - 1) if len(volumes) > 1 else 0.0
    rel_vol = volumes[-1] / avg_vol20 if avg_vol20 > 0 else 0.0
    uptrend = close > ema21 > ema50
    setup = "no_clear_setup"
    reasons: list[str] = []
    score = 0.0
    if uptrend:
        score += 2.0; reasons.append("close>EMA21>EMA50")
    if close > ema9 > ema21 > ema50 and macd_hist > 0 and 55 <= rsi14 <= 72:
        setup = "momentum_continuation"; score += 3.0; reasons.append("momentum EMA/MACD/RSI")
    if uptrend and abs(close - ema21) / close <= 0.025 and rsi14 >= 48:
        setup = "pullback_ema21"; score += 3.2; reasons.append("pullback EMA21")
    if close >= resistance * 0.995 and rel_vol >= 1.2 and macd_hist > 0:
        setup = "breakout_volume"; score += 3.5; reasons.append("breakout volume")
    if close <= support * 1.03 and close > closes[-2] and rsi14 >= 45:
        setup = "support_bounce"; score += 2.6; reasons.append("rebond support")
    if asset_type == "etf" and uptrend and rsi14 >= 50 and macd_hist >= 0:
        if setup == "no_clear_setup":
            setup = "etf_trend_following"
        score += 2.0; reasons.append("ETF trend following")
    if 45 <= rsi14 <= 72:
        score += 1.0
    if rel_vol >= 0.8:
        score += 0.8
    if atr14 > 0 and atr14 / close <= 0.06:
        score += 0.7
    if setup == "no_clear_setup":
        score = min(score, 6.9)
    technical_score = round(min(score, 10.0), 2)
    confidence = technical_score
    bid, ask, _spread = quote_spread_bps(quote or {})
    entry = ask if ask > 0 else close
    if setup in {"pullback_ema21", "etf_trend_following"}:
        stop_loss = min(ema21 - atr14 * 0.6, support - atr14 * 0.2)
    elif setup == "breakout_volume":
        stop_loss = max(resistance - atr14 * 0.8, close - atr14 * 1.6)
    else:
        stop_loss = max(support - atr14 * 0.2, close - atr14 * 1.5)
    if stop_loss <= 0 or stop_loss >= entry or atr14 <= 0:
        stop_loss = entry * 0.97
    target_r = 2.2
    take_profit = entry + (entry - stop_loss) * target_r
    risk_reward = (take_profit - entry) / (entry - stop_loss) if entry > stop_loss else 0.0
    return {
        "agent": "Technical Analyst Agent",
        "symbol": symbol,
        "direction": "long",
        "setup": setup,
        "entry": round(entry, 4),
        "stop_loss": round(stop_loss, 4),
        "take_profit": round(take_profit, 4),
        "risk_reward": round(risk_reward, 2),
        "confidence": round(confidence, 2),
        "technical_score": technical_score,
        "ema9": round(ema9, 4),
        "ema21": round(ema21, 4),
        "ema50": round(ema50, 4),
        "rsi14": round(rsi14, 2),
        "macd": {"line": round(macd_line, 4), "signal": round(macd_signal, 4), "histogram": round(macd_hist, 4)},
        "volume_relative": round(rel_vol, 2),
        "atr14": round(atr14, 4),
        "support": round(support, 4),
        "resistance": round(resistance, 4),
        "invalidation_condition": "daily close below stop_loss or setup invalidated",
        "technical_summary": "; ".join(reasons) if reasons else "aucun setup technique clair",
    }

def scan_market_universe(
    assets: list[dict[str, Any]],
    bars: dict[str, dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    config: MarketScannerConfig,
    historical_bars: dict[str, list[dict[str, Any]]] | None = None,
    *,
    account_cash: float | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc).isoformat()
    rejected: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []
    historical_bars = historical_bars or {}

    for asset in assets:
        symbol = str(asset.get("symbol", "")).upper()
        if not is_allowed_asset(asset):
            rejected["asset_not_allowed"] += 1
            continue
        if is_unapproved_etf(asset):
            rejected["etf_not_approved"] += 1
            continue
        bar = bars.get(symbol) or {}
        quote = quotes.get(symbol) or {}
        price = finite_float(bar.get("c") or bar.get("close"))
        volume = int(finite_float(bar.get("v") or bar.get("volume")))
        bid, ask, spread_bps = quote_spread_bps(quote)

        # Fallback: if bar had no close (Alpaca multi-symbol limit issue), use quote midpoint
        if price <= 0 and bid > 0 and ask > 0:
            price = (bid + ask) / 2.0
        elif price <= 0 and ask > 0:
            price = ask
        elif price <= 0 and bid > 0:
            price = bid

        dollar_volume = price * volume

        if price < config.min_price or price > config.max_price:
            rejected["price_out_of_range"] += 1
            continue
        if volume < config.min_volume or dollar_volume < config.min_dollar_volume:
            rejected["liquidity_too_low"] += 1
            continue
        if not math.isfinite(spread_bps) or spread_bps > config.max_spread_bps:
            rejected["spread_too_wide"] += 1
            continue
        if account_cash is not None and price > account_cash and config.require_fractionable_over_cash and not asset.get("fractionable"):
            rejected["not_fractionable_for_cash"] += 1
            continue

        score = (math.log10(max(dollar_volume, 1)) * 10) - spread_bps
        candidate_asset_type = asset_type(asset)
        technical_analysis = analyze_technicals(symbol, candidate_asset_type, historical_bars.get(symbol, []), quote)
        candidates.append(
            {
                "symbol": symbol,
                "asset_type": candidate_asset_type,
                "exchange": asset.get("exchange"),
                "fractionable": bool(asset.get("fractionable")),
                "price": round(price, 4),
                "bid": round(bid, 4),
                "ask": round(ask, 4),
                "spread_bps": round(spread_bps, 3),
                "volume": volume,
                "dollar_volume": round(dollar_volume, 2),
                "liquidity_score": round(score, 3),
                "technical_score": technical_analysis.get("technical_score", 0),
                "risk_reward": technical_analysis.get("risk_reward", 0),
                "confidence": technical_analysis.get("confidence", 0),
                "last_price": round(price, 4),
                "entry": technical_analysis.get("entry", 0),
                "stop_loss": technical_analysis.get("stop_loss", 0),
                "take_profit": technical_analysis.get("take_profit", 0),
                "setup": technical_analysis.get("setup"),
                "trend": technical_analysis.get("technical_summary", "n/a"),
                "technical_analysis": technical_analysis,
                "catalyst_analysis": {"agent": "Catalyst & News Agent", "symbol": symbol, "catalyst_status": "no_verified_catalyst", "summary": "No verified catalyst", "risk_level": 0, "trade_allowed": True, "score": 6.0},
                "catalyst_score": 6.0,
                "catalyst": "No verified catalyst",
                "action": "watch_only_no_order",
            }
        )

    candidates.sort(key=lambda item: (item["liquidity_score"], item["dollar_volume"]), reverse=True)
    candidates = candidates[: config.top_n]

    return {
        "agent": "Market Scanner Agent",
        "timestamp": generated_at,
        "orders_sent": 0,
        "market_scanner": {
            "mode": "prudent_watchlist_only",
            "universe": {
                "allowed_asset_classes": ["us_equity"],
                "allowed_etfs": sorted(APPROVED_LIQUID_ETFS),
                "allowed_exchanges": sorted(MAJOR_US_EXCHANGES),
                "assets_seen": len(assets),
            },
            "criteria": dataclasses.asdict(config),
            "candidates": candidates,
            "opportunities_found": len(candidates),
            "rejected_counts": dict(sorted(rejected.items())),
            "compliance": {"orders_sent": 0, "order_endpoints_called": False, "read_only": True},
        },
    }


def choose_asset_universe(assets: list[dict[str, Any]], max_universe: int) -> list[dict[str, Any]]:
    allowed = [asset for asset in assets if is_allowed_asset(asset) and not is_unapproved_etf(asset)]
    # Prefer fractionable major listings and approved liquid ETFs, then cap to avoid
    # overlong market-data requests during early automation.
    allowed.sort(
        key=lambda asset: (
            str(asset.get("symbol", "")).upper() in APPROVED_LIQUID_ETFS,
            bool(asset.get("fractionable")),
            str(asset.get("symbol", "")),
        ),
        reverse=True,
    )
    return allowed[:max_universe]


def run_live_scan() -> dict[str, Any]:
    policy = load_policy()
    config = MarketScannerConfig.from_policy(policy)
    client = AlpacaReadOnlyClient()
    if not client.credentials_present:
        return {
            "agent": "Market Scanner Agent",
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "orders_sent": 0,
            "market_scanner": {
                "mode": "blocked_missing_credentials",
                "candidates": [],
                "opportunities_found": 0,
                "rejected_counts": {},
                "compliance": {"orders_sent": 0, "order_endpoints_called": False, "read_only": True},
                "reason": "missing_alpaca_credentials",
            },
        }

    if "paper-api.alpaca.markets" not in client.trading_base_url:
        raise RuntimeError("Refusing to scan unless APCA_API_BASE_URL points to Alpaca Paper")

    account = client.account()
    account_cash = finite_float(account.get("cash"))
    universe_assets = choose_asset_universe(client.assets(), config.max_universe)
    symbols = [str(asset["symbol"]).upper() for asset in universe_assets]
    bars = client.latest_daily_bars(symbols, config.feed)
    quotes = client.latest_quotes(symbols, config.feed)
    initial = scan_market_universe(universe_assets, bars, quotes, config, account_cash=account_cash)
    candidate_symbols = [item["symbol"] for item in initial.get("market_scanner", {}).get("candidates", [])]
    historical = client.historical_daily_bars(candidate_symbols, config.feed, limit=120) if candidate_symbols else {}
    result = scan_market_universe(universe_assets, bars, quotes, config, historical_bars=historical, account_cash=account_cash)
    result["account_snapshot"] = {"cash": round(account_cash, 2), "paper_endpoint": client.trading_base_url}
    return result


def append_journal(result: dict[str, Any]) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "agent": "Market Scanner Agent",
        "event_type": "market_scan",
        "symbol": "N/A",
        "timestamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": f"Scan prudent terminé; {result.get('market_scanner', {}).get('opportunities_found', 0)} candidats; aucun ordre.",
        "data": result,
    }
    with JOURNAL.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_report(result: dict[str, Any]) -> pathlib.Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    report = REPORTS / f"market_scan_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return report


def main() -> int:
    try:
        result = run_live_scan()
        append_journal(result)
        write_report(result)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("market_scanner", {}).get("mode") != "blocked_missing_credentials" else 2
    except urllib.error.HTTPError as exc:
        print(json.dumps({"agent": "Market Scanner Agent", "orders_sent": 0, "error": f"http_error_{exc.code}"}), file=sys.stderr)
        return 3
    except Exception as exc:
        print(json.dumps({"agent": "Market Scanner Agent", "orders_sent": 0, "error": type(exc).__name__, "reason": str(exc)}), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())

