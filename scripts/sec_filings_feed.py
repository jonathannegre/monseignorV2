#!/usr/bin/env python3
"""SEC EDGAR filing normalizer for catalyst enrichment."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import urllib.request
from typing import Any, Mapping

SUBMISSIONS_URL = 'https://data.sec.gov/submissions/CIK{cik:010d}.json'
USER_AGENT = 'MonseignorV2 catalyst research contact=github.com/jonathannegre/monseignorV2'
RISK_FORMS = {'S-3', 'S-3ASR', '424B2', '424B3', '424B5', 'NT 10-Q', 'NT 10-K'}
POSITIVE_FORMS = {'13D', '13G', '4'}


def classify_filing(form: str, primary_doc: str = '', description: str = '') -> dict[str, Any]:
    text = f'{form} {primary_doc} {description}'.lower()
    form = form.upper()
    if form in RISK_FORMS:
        return {'event_type': 'offering_dilution' if form.startswith(('S-3', '424B')) else 'delayed_filing', 'sentiment': -0.75, 'materiality': 0.9}
    if form == '8-K':
        if any(term in text for term in ('departure', 'resignation', 'delisting', 'bankruptcy', 'investigation', 'going concern')):
            return {'event_type': 'sec_probe', 'sentiment': -0.65, 'materiality': 0.85}
        return {'event_type': 'material_8k', 'sentiment': 0.05, 'materiality': 0.7}
    if form in POSITIVE_FORMS:
        return {'event_type': 'insider_or_activist_filing', 'sentiment': 0.35, 'materiality': 0.65}
    return {'event_type': 'sec_filing', 'sentiment': 0.0, 'materiality': 0.45}


def normalize_filing(symbol: str, filing: Mapping[str, Any]) -> dict[str, Any]:
    form = str(filing.get('form', '')).upper()
    filed_at = str(filing.get('filingDate') or filing.get('acceptanceDateTime') or dt.date.today().isoformat())
    cls = classify_filing(form, str(filing.get('primaryDocument', '')), str(filing.get('primaryDocDescription', '')))
    return {'symbol': symbol.upper(), 'published_at': filed_at if 'T' in filed_at else filed_at + 'T00:00:00Z', 'source': 'sec_edgar', 'source_tier': 1, 'event_type': cls['event_type'], 'headline': f'{symbol.upper()} SEC {form} filed {filed_at}', 'summary': str(filing.get('primaryDocDescription') or form), 'url': str(filing.get('url', '')), 'sentiment': cls['sentiment'], 'relevance': 1.0, 'materiality': cls['materiality'], 'metadata': {'form': form, 'accessionNumber': filing.get('accessionNumber'), 'primaryDocument': filing.get('primaryDocument')}}


def write_jsonl(events: list[Mapping[str, Any]], path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        for event in events:
            f.write(json.dumps(dict(event), ensure_ascii=False) + '\n')


def _urlopen_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


def fetch_recent_filings(symbol: str, cik: int, *, limit: int = 20) -> list[dict[str, Any]]:
    data = _urlopen_json(SUBMISSIONS_URL.format(cik=cik))
    recent = data.get('filings', {}).get('recent', {}) if isinstance(data, dict) else {}
    filings = []
    for i, form in enumerate(recent.get('form', [])[:limit]):
        filing = {k: (v[i] if isinstance(v, list) and i < len(v) else None) for k, v in recent.items()}
        filings.append(normalize_filing(symbol, filing))
    return filings
