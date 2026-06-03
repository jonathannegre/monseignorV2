#!/usr/bin/env python3
"""Catalyst & News Agent v1 for Monseignor.

The agent is deterministic and testable: it can score supplied news items during
unit tests, and in live mode can consume a local JSON/JSONL news cache without
requiring a paid provider. Missing news no longer invents a fake catalyst; it is
reported explicitly as neutral/macro-only context.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
from typing import Any, Iterable, Mapping

TRUSTED_SOURCES = {"reuters", "bloomberg", "associated press", "ap", "dow jones", "wsj", "sec", "nasdaq", "nyse"}
POSITIVE_TERMS = {
    "beat": 1.2,
    "beats": 1.2,
    "raises guidance": 1.5,
    "upgrade": 0.9,
    "partnership": 0.8,
    "contract": 0.8,
    "approval": 1.0,
    "buyback": 0.8,
    "record revenue": 1.1,
    "launches": 0.5,
    "ai": 0.4,
}
NEGATIVE_TERMS = {
    "fraud": 2.5,
    "investigation": 1.5,
    "sec probe": 2.0,
    "guidance cut": 1.8,
    "misses": 1.5,
    "downgrade": 1.0,
    "lawsuit": 1.2,
    "recall": 1.3,
    "bankruptcy": 3.0,
    "halt": 2.0,
}
MATERIAL_TERMS = {"earnings", "guidance", "8-k", "sec", "merger", "acquisition", "contract", "approval", "partnership"}
MACRO_ETFS = {"SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "XLI", "XLP", "XLU", "XLB", "XLRE", "XLC", "EEM", "VEA", "VWO", "TLT", "IEF"}


@dataclasses.dataclass(frozen=True)
class NewsItem:
    symbol: str
    headline: str
    source: str = "unknown"
    published_at: dt.datetime | None = None
    url: str = ""
    summary: str = ""
    event_type: str = ""
    sentiment: float | None = None
    relevance: float | None = None
    materiality: float | None = None
    source_tier: int | None = None
    metadata: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "NewsItem":
        published = raw.get("published_at") or raw.get("created_at") or raw.get("timestamp")
        parsed: dt.datetime | None = None
        if published:
            try:
                parsed = dt.datetime.fromisoformat(str(published).replace("Z", "+00:00"))
            except Exception:
                parsed = None
        return cls(
            symbol=str(raw.get("symbol", "")).upper(),
            headline=str(raw.get("headline") or raw.get("title") or ""),
            source=str(raw.get("source", "unknown")),
            published_at=parsed,
            url=str(raw.get("url", "")),
            summary=str(raw.get("summary", "")),
            event_type=str(raw.get("event_type", "")),
            sentiment=_optional_float(raw.get("sentiment")),
            relevance=_optional_float(raw.get("relevance")),
            materiality=_optional_float(raw.get("materiality")),
            source_tier=_optional_int(raw.get("source_tier")),
            metadata=raw.get("metadata", {}) if isinstance(raw.get("metadata", {}), Mapping) else {},
        )


def _optional_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _optional_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


class CatalystAgent:
    def __init__(self, news_items: Iterable[NewsItem | Mapping[str, Any]] | None = None, *, now: dt.datetime | None = None, max_age_hours: int = 72) -> None:
        self.now = now or dt.datetime.now(dt.timezone.utc)
        if self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=dt.timezone.utc)
        self.max_age_hours = max_age_hours
        self.news_items = [item if isinstance(item, NewsItem) else NewsItem.from_mapping(item) for item in (news_items or [])]

    @classmethod
    def from_json_file(cls, path: pathlib.Path, *, now: dt.datetime | None = None) -> "CatalystAgent":
        if not path.exists():
            return cls(now=now)
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return cls(now=now)
        if path.suffix == ".jsonl":
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        else:
            parsed = json.loads(raw)
            rows = parsed if isinstance(parsed, list) else parsed.get("items", []) if isinstance(parsed, dict) else []
        return cls(rows, now=now)

    def _fresh_items(self, symbol: str) -> list[NewsItem]:
        sym = symbol.upper()
        items: list[NewsItem] = []
        for item in self.news_items:
            if item.symbol.upper() != sym:
                continue
            if item.published_at is None:
                items.append(item)
                continue
            published = item.published_at if item.published_at.tzinfo else item.published_at.replace(tzinfo=dt.timezone.utc)
            age_hours = (self.now - published).total_seconds() / 3600.0
            if age_hours <= self.max_age_hours:
                items.append(item)
        return items

    def score_symbol(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        items = self._fresh_items(symbol)
        if not items:
            status = "macro_context_only" if symbol in MACRO_ETFS else "no_verified_catalyst"
            return {
                "agent": "Catalyst & News Agent",
                "symbol": symbol,
                "catalyst_status": status,
                "summary": "Macro/sector context only" if status == "macro_context_only" else "No verified catalyst",
                "risk_level": 0,
                "trade_allowed": True,
                "score": 6.0 if status == "macro_context_only" else 5.0,
                "headlines": [],
            }

        positive = 0.0
        negative = 0.0
        material_hits = 0
        trusted_hits = 0
        headlines: list[dict[str, Any]] = []
        seen_headlines: set[str] = set()
        novelty_bonus = 0.0
        structured_event_hits: list[str] = []
        hard_veto_events = {
            "fraud_investigation",
            "sec_probe",
            "guidance_cut",
            "earnings_miss",
            "offering_dilution",
            "bankruptcy",
            "analyst_downgrade",
        }
        for item in items:
            text = f"{item.headline} {item.summary}".lower()
            source = item.source.lower()
            if item.headline.lower() not in seen_headlines:
                novelty_bonus += 0.15
                seen_headlines.add(item.headline.lower())
            trusted = any(src in source for src in TRUSTED_SOURCES) or item.source_tier == 1 or "finnhub" in source
            trusted_hits += 1 if trusted else 0
            for term, weight in POSITIVE_TERMS.items():
                if term in text:
                    positive += weight
            for term, weight in NEGATIVE_TERMS.items():
                if term in text:
                    negative += weight
            if item.sentiment is not None:
                if item.sentiment > 0:
                    positive += min(item.sentiment * 1.6, 1.6)
                else:
                    negative += min(abs(item.sentiment) * 1.8, 1.8)
            if item.materiality is not None:
                material_hits += 1 if item.materiality >= 0.5 else 0
                positive += max(0.0, item.materiality - 0.5) * 0.6 if (item.sentiment or 0) >= 0 else 0.0
                negative += max(0.0, item.materiality - 0.5) * 0.8 if (item.sentiment or 0) < 0 else 0.0
            else:
                material_hits += 1 if any(term in text for term in MATERIAL_TERMS) else 0
            if item.event_type:
                structured_event_hits.append(item.event_type)
            headlines.append({"headline": item.headline, "source": item.source, "url": item.url, "event_type": item.event_type})

        source_bonus = min(trusted_hits * 0.25, 0.75)
        material_bonus = min(material_hits * 0.35, 1.0)
        raw_score = 5.0 + positive + source_bonus + material_bonus + novelty_bonus - negative * 1.4
        score = round(max(0.0, min(10.0, raw_score)), 2)
        negative_veto = (negative >= 2.0 and negative > positive) or any(event in hard_veto_events for event in structured_event_hits)
        status = "negative_news_veto" if negative_veto else "positive_catalyst" if score >= 7.0 else "mixed_or_weak_catalyst"
        summary = "; ".join(h["headline"] for h in headlines[:3])
        return {
            "agent": "Catalyst & News Agent",
            "symbol": symbol,
            "catalyst_status": status,
            "summary": summary,
            "risk_level": round(negative, 2),
            "trade_allowed": not negative_veto,
            "score": score,
            "positive_signal": round(positive, 2),
            "negative_signal": round(negative, 2),
            "material_hits": material_hits,
            "trusted_source_hits": trusted_hits,
            "structured_event_hits": structured_event_hits,
            "headlines": headlines,
        }


def score_catalysts_for_symbols(symbols: Iterable[str], agent: CatalystAgent | None = None) -> dict[str, dict[str, Any]]:
    agent = agent or CatalystAgent()
    return {symbol.upper(): agent.score_symbol(symbol) for symbol in symbols}
