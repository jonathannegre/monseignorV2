# Monseignor Finnhub catalyst feed

Monseignor now supports Finnhub as the first live catalyst/news provider. The API key must stay outside git.

## Secret placement

Create this ignored file:

```bash
mkdir -p .secrets
chmod 700 .secrets
printf 'FINNHUB_API_KEY=...\n' > .secrets/finnhub.env
chmod 600 .secrets/finnhub.env
```

The current local VM already has `.secrets/finnhub.env` populated.

## Manual refresh

```bash
python3 scripts/finnhub_catalyst_feed.py \
  --symbols AAPL,NVDA,MSFT,TSLA \
  --env-file .secrets/finnhub.env \
  --cache-path data/news_cache.jsonl
```

The output cache is JSONL and gitignored: `data/news_cache.jsonl`.

## Autonomous run behavior

`crons/picsou_autonomous_trader.sh` sources `.secrets/finnhub.env` when present. During `run_live_scan()` Monseignor:

1. performs the initial technical/liquidity scan,
2. extracts the candidate symbols,
3. refreshes Finnhub company news + earnings calendar for those candidates,
4. reloads `data/news_cache.jsonl`,
5. re-runs the candidate scoring with structured catalyst data.

This keeps Finnhub usage focused on tradable candidates instead of wasting calls on the full Alpaca universe.

## Normalized event fields

Each event contains:

- `symbol`
- `published_at`
- `source`
- `provider_source`
- `source_tier`
- `event_type`
- `headline`
- `summary`
- `url`
- `sentiment`
- `relevance`
- `materiality`
- `entities`
- `metadata`

The Catalyst Agent uses structured `event_type`, `sentiment`, `materiality`, and `source_tier` in addition to keyword scoring. Hard-veto events include fraud investigation, SEC probe, guidance cut, earnings miss, offering/dilution, bankruptcy, and analyst downgrade.
