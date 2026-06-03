# Monseignor upgrade wave 1

Implemented on 2026-06-03.

## What changed

- Catalyst Agent v1 (`scripts/catalyst_agent.py`): deterministic real-news scoring from local JSON/JSONL cache, trusted-source weighting, positive catalyst boosts, and hard negative-news veto.
- Historical backtester (`scripts/backtester.py`): setup-level R-multiple backtests, walk-forward folds, and setup-rotation recommendations.
- Position Manager v1 (`scripts/position_manager.py`): ATR trailing stop, breakeven promotion after +1R, partial profit action after +1.5R, time-stop and regime-flip actions.
- Portfolio Construction Agent (`scripts/portfolio_constructor.py`): conviction ranking, sector caps, regime budget, max-new-order cap, and rejected-proposal audit trail.
- Intraday Execution Agent (`scripts/intraday_execution.py`): 5m confirmation primitives, opening spread guard, spread/relative-volume checks, stale order repricing/cancel rules.
- Decision Replay (`scripts/decision_replay.py`): one replayable JSON decision object per cycle with inputs, candidate scores, vetoes, selected allocation, and submitted order IDs.

## Integration points

- `market_scanner.py` now uses the Catalyst Agent instead of a fixed placeholder score.
- `trade_validation_pipeline.py` now applies the negative-catalyst veto and routes approved proposals through Portfolio Construction.
- `daily_cycle.py` writes a replayable decision snapshot under `reports/decisions/` each run.
- `config/policy.json` now contains configuration sections for catalyst, portfolio construction, intraday execution, position management, and decision replay.
- `data/news_cache.example.jsonl` documents the local news-cache format; real credentials are not required and are not stored.

## Validation

- Added `tests/test_monseignor_upgrade_wave.py` covering all new agents and pipeline integration.
- Full suite passes: `python3 -m pytest -q` → 28 passed.

## Notes for paper onboarding

This wave prepares Monseignor for a dedicated Paper account but does not require one. The code remains Paper-only through the existing Alpaca endpoint checks. Before assigning capital, run at least one shadow cycle with the dedicated credentials and inspect the generated decision snapshot.
