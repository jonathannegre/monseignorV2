# MonseignorV2 upgrade wave

MonseignorV2 is the isolated successor fork of Monseignor. It keeps trading disabled until Jo provides the dedicated Alpaca Paper key.

Implemented V2 controls:

1. Broker-visible stop audit
- Every cycle can fetch Alpaca positions and open orders.
- A position without a visible sell stop becomes a critical incident.
- The repair layer builds broker stop payloads and can auto-submit only when execution is explicitly authorized.

2. Micro-order and cash discipline
- New buys are blocked when usable cash is below `cash_control.min_new_buy_cash_usd`.
- The bot switches to rotation-only mode instead of sending tiny rejected Alpaca orders.

3. Hold / trim / exit / replace scoring
- Existing positions are scored with P&L, exposure, catalyst status, technical score, and age.
- Actions are explicit: HOLD, TRIM, EXIT, REPLACE_WITH.
- Replacement requires a candidate score margin over the weak existing holding.

4. ETF catalyst quality filter
- Generic market-wrap headlines are discounted for ETFs.
- Sector-specific ETF catalysts receive more weight.

5. SEC filings enrichment
- `scripts/sec_filings_feed.py` normalizes EDGAR filings into the same JSONL catalyst event schema as Finnhub.
- Dilution/offering forms become `offering_dilution`; risky 8-K text can become `sec_probe`.

6. Real setup rotation discipline
- Policy is reset to real closed trades or backtest only.
- Proxy stats from proposal-only events are no longer used as live truth.

7. Performance attribution
- Closed-trade records can be grouped by setup, catalyst status, and sector.

8. Objective-aware risk
- Policy keeps objective metadata but resets activation state for a dedicated MonseignorV2 paper account.

Activation checklist when Jo provides keys:
- Store Alpaca credentials in `.secrets/alpaca-paper.env` chmod 600.
- Force Alpaca Paper cash-only/no-shorting account configuration.
- Run `python3 -m pytest -q`.
- Run `python3 scripts/check_alpaca_account.py`.
- Run `python3 scripts/order_executor.py` in dry-run mode.
- Only then flip `execution_authorization.authorized_by_user` and `alpaca_paper_orders_after_full_pipeline` to true.
