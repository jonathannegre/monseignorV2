#!/usr/bin/env python3
"""Fetch and normalize Finnhub catalyst data for Monseignor.

This module deliberately keeps the API token out of tracked files. Live runs read
FINNHUB_API_KEY from the environment (typically .secrets/finnhub.env sourced by
cron) and write normalized JSONL to data/news_cache.jsonl, which is gitignored.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable, Mapping

BASE = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CACHE = BASE / "data" / "news_cache.jsonl"
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"

POSITIVE_PATTERNS = (
    ("earnings_beat_guidance_raise", ("beat", "raises guidance"), 0.9, 0.95),
    ("earnings_beat_guidance_raise", ("beats", "raises guidance"), 0.9, 0.95),
    ("earnings_beat", ("beat", "earnings"), 0.75, 0.85),
    ("earnings_beat", ("beats", "earnings"), 0.75, 0.85),
    ("analyst_upgrade", ("upgrade",), 0.65, 0.7),
    ("major_contract", ("contract",), 0.6, 0.75),
    ("strategic_partnership", ("partnership",), 0.55, 0.65),
    ("product_launch", ("launch",), 0.35, 0.45),
    ("fda_approval", ("approval",), 0.7, 0.85),
    ("buyback", ("buyback",), 0.55, 0.65),
)
NEGATIVE_PATTERNS = (
    ("fraud_investigation", ("fraud", "investigation"), -0.95, 0.95),
    ("sec_probe", ("sec probe",), -0.9, 0.9),
    ("guidance_cut", ("guidance cut",), -0.8, 0.85),
    ("earnings_miss", ("misses", "earnings"), -0.7, 0.8),
    ("analyst_downgrade", ("downgrade",), -0.55, 0.65),
    ("lawsuit", ("lawsuit",), -0.55, 0.65),
    ("bankruptcy", ("bankruptcy",), -1.0, 1.0),
    ("offering_dilution", ("offering",), -0.75, 0.8),
)


def _utc_iso_from_epoch(epoch: int | float | str | None, fallback: dt.datetime | None = None) -> str:
    if epoch:
        try:
            return dt.datetime.fromtimestamp(float(epoch), tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception:
            pass
    fallback = fallback or dt.datetime.now(dt.timezone.utc)
    if fallback.tzinfo is None:
        fallback = fallback.replace(tzinfo=dt.timezone.utc)
    return fallback.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _classify_text(headline: str, summary: str = "") -> tuple[str, float, float]:
    text = f"{headline} {summary}".lower()
    for event_type, terms, sentiment, materiality in NEGATIVE_PATTERNS:
        if all(term in text for term in terms):
            return event_type, sentiment, materiality
    for event_type, terms, sentiment, materiality in POSITIVE_PATTERNS:
        if all(term in text for term in terms):
            return event_type, sentiment, materiality
    material_terms = ("earnings", "guidance", "merger", "acquisition", "contract", "8-k", "sec", "fda", "partnership")
    if any(term in text for term in material_terms):
        return "material_news", 0.15, 0.55
    return "company_news", 0.0, 0.25


def normalize_company_news_item(symbol: str, item: Mapping[str, Any]) -> dict[str, Any]:
    headline = str(item.get("headline") or "").strip()
    summary = str(item.get("summary") or "").strip()
    event_type, sentiment, materiality = _classify_text(headline, summary)
    related = item.get("related") or ""
    entities = [s.strip().upper() for s in str(related).split(",") if s.strip()]
    if symbol.upper() not in entities:
        entities.insert(0, symbol.upper())
    return {
        "symbol": symbol.upper(),
        "published_at": _utc_iso_from_epoch(item.get("datetime")),
        "source": "finnhub",
        "provider_source": str(item.get("source") or "Finnhub"),
        "source_tier": 1,
        "event_type": event_type,
        "headline": headline,
        "summary": summary,
        "url": str(item.get("url") or ""),
        "sentiment": sentiment,
        "relevance": 0.9 if symbol.upper() in entities else 0.65,
        "materiality": materiality,
        "entities": entities,
        "metadata": {"category": item.get("category"), "image": item.get("image")},
    }


def normalize_earnings_calendar_item(item: Mapping[str, Any], *, now: dt.datetime | None = None) -> dict[str, Any]:
    symbol = str(item.get("symbol") or "").upper()
    date = str(item.get("date") or "")
    hour = str(item.get("hour") or "").lower()
    eps_actual = _safe_float(item.get("epsActual"))
    eps_estimate = _safe_float(item.get("epsEstimate"))
    rev_actual = _safe_float(item.get("revenueActual"))
    rev_estimate = _safe_float(item.get("revenueEstimate"))
    eps_surprise = _surprise_pct(eps_actual, eps_estimate)
    rev_surprise = _surprise_pct(rev_actual, rev_estimate)
    positive = (eps_surprise or 0) > 5 or (rev_surprise or 0) > 5
    negative = (eps_surprise or 0) < -5 or (rev_surprise or 0) < -5
    event_type = "earnings_miss" if negative and not positive else "earnings_beat" if positive else "earnings_calendar"
    sentiment = -0.75 if event_type == "earnings_miss" else 0.8 if event_type == "earnings_beat" else 0.05
    materiality = 0.9 if event_type in {"earnings_miss", "earnings_beat"} else 0.65
    suffix = "after market close" if hour == "amc" else "before market open" if hour == "bmo" else "scheduled"
    headline = f"{symbol} earnings {event_type.replace('_', ' ')} ({suffix})".strip()
    parts = []
    if eps_surprise is not None:
        parts.append(f"EPS surprise {eps_surprise:.1f}%")
    if rev_surprise is not None:
        parts.append(f"Revenue surprise {rev_surprise:.1f}%")
    published_at = _date_to_iso(date, hour, now=now)
    return {
        "symbol": symbol,
        "published_at": published_at,
        "source": "finnhub_earnings_calendar",
        "provider_source": "Finnhub earnings calendar",
        "source_tier": 1,
        "event_type": event_type,
        "headline": headline,
        "summary": "; ".join(parts) or f"Earnings event for {symbol} on {date}",
        "url": "",
        "sentiment": sentiment,
        "relevance": 1.0,
        "materiality": materiality,
        "entities": [symbol],
        "metadata": dict(item),
    }


def _date_to_iso(date_text: str, hour: str, *, now: dt.datetime | None = None) -> str:
    try:
        date = dt.date.fromisoformat(date_text)
        hour_utc = 21 if hour == "amc" else 12 if hour == "bmo" else 16
        return dt.datetime(date.year, date.month, date.day, hour_utc, tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return _utc_iso_from_epoch(None, now)


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _surprise_pct(actual: float | None, estimate: float | None) -> float | None:
    if actual is None or estimate in (None, 0):
        return None
    return (actual - estimate) / abs(estimate) * 100.0


def build_news_cache(
    *,
    symbols: Iterable[str],
    company_news_by_symbol: Mapping[str, Iterable[Mapping[str, Any]]],
    earnings_rows: Iterable[Mapping[str, Any]],
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    wanted = {s.upper() for s in symbols if s}
    rows: list[dict[str, Any]] = []
    for symbol in sorted(wanted):
        for item in company_news_by_symbol.get(symbol, []):
            normalized = normalize_company_news_item(symbol, item)
            if normalized["headline"]:
                rows.append(normalized)
    for item in earnings_rows:
        symbol = str(item.get("symbol") or "").upper()
        if symbol in wanted:
            rows.append(normalize_earnings_calendar_item(item, now=now))
    return rows


def write_jsonl_cache(rows: Iterable[Mapping[str, Any]], path: pathlib.Path = DEFAULT_CACHE) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    unique: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for row in rows:
        key = (str(row.get("symbol", "")).upper(), str(row.get("headline", "")), str(row.get("url", "")))
        unique[key] = row
    sorted_rows = sorted(unique.values(), key=lambda r: str(r.get("published_at", "")), reverse=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in sorted_rows), encoding="utf-8")
    return len(sorted_rows)


class FinnhubClient:
    def __init__(self, token: str, *, opener: Callable[[str], Any] | None = None, base_url: str = FINNHUB_BASE_URL) -> None:
        if not token:
            raise ValueError("FINNHUB_API_KEY is required")
        self.token = token
        self.opener = opener or urllib.request.urlopen
        self.base_url = base_url.rstrip("/")

    def _get(self, endpoint: str, params: Mapping[str, Any]) -> Any:
        query = urllib.parse.urlencode({**params, "token": self.token})
        url = f"{self.base_url}/{endpoint}?{query}"
        with self.opener(url) as response:
            return json.loads(response.read().decode("utf-8"))

    def company_news(self, symbol: str, from_date: dt.date, to_date: dt.date) -> list[dict[str, Any]]:
        data = self._get("company-news", {"symbol": symbol.upper(), "from": from_date.isoformat(), "to": to_date.isoformat()})
        return data if isinstance(data, list) else []

    def earnings_calendar(self, from_date: dt.date, to_date: dt.date) -> list[dict[str, Any]]:
        data = self._get("calendar/earnings", {"from": from_date.isoformat(), "to": to_date.isoformat()})
        rows = data.get("earningsCalendar", []) if isinstance(data, dict) else []
        return rows if isinstance(rows, list) else []


def fetch_and_write_cache(
    symbols: Iterable[str],
    *,
    token: str,
    cache_path: pathlib.Path = DEFAULT_CACHE,
    lookback_days: int = 7,
    sleep_seconds: float = 0.25,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date()
    from_date = today - dt.timedelta(days=lookback_days)
    client = FinnhubClient(token)
    symbols = sorted({s.upper() for s in symbols if s})
    company_news: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    for symbol in symbols:
        try:
            company_news[symbol] = client.company_news(symbol, from_date, today)
        except Exception as exc:  # keep cycle alive if one symbol/provider call fails
            company_news[symbol] = []
            errors[symbol] = str(exc)[:200]
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    try:
        earnings = client.earnings_calendar(from_date, today + dt.timedelta(days=7))
    except Exception as exc:
        earnings = []
        errors["earnings_calendar"] = str(exc)[:200]
    rows = build_news_cache(symbols=symbols, company_news_by_symbol=company_news, earnings_rows=earnings, now=now)
    written = write_jsonl_cache(rows, cache_path)
    return {"symbols": symbols, "events_written": written, "cache_path": str(cache_path), "errors": errors}


def _load_env_file(path: pathlib.Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh Monseignor Finnhub catalyst cache")
    parser.add_argument("--symbols", required=True, help="Comma-separated ticker symbols")
    parser.add_argument("--cache-path", default=str(DEFAULT_CACHE))
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--env-file", default=str(BASE / ".secrets" / "finnhub.env"))
    args = parser.parse_args(argv)
    _load_env_file(pathlib.Path(args.env_file))
    token = os.environ.get("FINNHUB_API_KEY", "")
    if not token:
        print(json.dumps({"error": "missing_FINNHUB_API_KEY"}), file=sys.stderr)
        return 2
    result = fetch_and_write_cache(
        [s.strip() for s in args.symbols.split(",") if s.strip()],
        token=token,
        cache_path=pathlib.Path(args.cache_path),
        lookback_days=args.lookback_days,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
