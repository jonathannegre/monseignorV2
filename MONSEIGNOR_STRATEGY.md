# Monseignor strategy assessment

Date: 2026-06-03
Source fork: jonathannegre/picsou, local source /home/hermes/projects/picsou-alpaca
Target repo: https://github.com/jonathannegre/monseignor

## Fork status

Monseignor starts as a fork of the Picsou Alpaca monolith, with history preserved.
The local copy is /home/hermes/projects/monseignor and origin points to jonathannegre/monseignor.
The first preparation pass made the code independent from the Picsou path by deriving BASE from the script location and by changing cron paths to /home/hermes/projects/monseignor.

## Current Picsou baseline observed

Account snapshot from Alpaca Paper, read-only check:
- Portfolio value: about $10,381
- Cash: about $5, nearly fully deployed
- Open positions: AMD, EEM, IWM, NVDA, TQQQ, XLF
- Open protective stop orders: 5
- Journal size: 1,866 events
- Validation proposals: 99 total
- Order submissions recorded: 6 submitted, 57 rejected, 24 order errors
- Dominant setup labels in journal: no_clear_setup 641, support_bounce 332, pullback_ema21 193, momentum_continuation 69, etf_trend_following 34, breakout_volume 13

Interpretation: Picsou is now aggressive and deployed, but its alpha engine is still mostly deterministic technical scoring with placeholder catalyst scoring, limited learning, and a monolithic daily-cycle control loop.

## Priority improvements for Monseignor

### P0 — Make Monseignor structurally independent and test-clean

Why it matters: a fork that writes to Picsou paths or reads Picsou policy/journal cannot be safely evolved.

Implementation targets:
- Keep BASE path relative to each script, not hard-coded to /home/hermes/projects/picsou-alpaca.
- Rename runtime artifacts, cron comments, report labels, and project metadata from Picsou to Monseignor.
- Add a sample .secrets template without credentials.
- Fix stale tests so CI catches path regressions.

Acceptance:
- `python3 -m pytest -q` passes in /home/hermes/projects/monseignor.
- Running scripts from Monseignor writes only under /home/hermes/projects/monseignor.

### P1 — Replace placeholder catalyst scoring with real news/earnings/macro signals

Problem in current code: every scanner candidate gets a synthetic catalyst score of 6.0 and `No verified catalyst`. That means the system ranks mostly by liquidity plus technicals, ignoring the events that create outsized moves.

Build:
- News ingestion per symbol: headlines, source credibility, recency, sentiment, novelty.
- Earnings calendar and post-earnings drift detection.
- SEC/8-K/material news detection for US equities.
- Macro/sector regime context for ETF-heavy trades.
- A hard negative-news veto and a positive-catalyst boost.

Expected impact: better selection quality, fewer trades in technically clean but catalyst-dead names, more exposure to names with a real reason to move.

### P2 — Add real backtesting and walk-forward evaluation before changing live policy

Problem: setup_rotation currently learns from sparse live history and has older proxy expectancy data mixed with real fills. Live-only learning is too slow and statistically fragile.

Build:
- Historical bars backtester for the exact scanner setups: pullback_ema21, breakout_volume, support_bounce, momentum_continuation, ETF trend following.
- Walk-forward split by quarter/month to avoid overfitting.
- Metrics per setup and symbol class: win rate, average R, max drawdown, time in trade, hit rate of stop/take-profit.
- Export policy recommendations only if sample size and out-of-sample performance are good.

Expected impact: faster iteration and fewer blind policy changes.

### P3 — Improve position management: exits should be dynamic, not only static stops

Problem: current implementation renews stop orders, but fractional positions use simple limits and then separate stop renewal. Take-profit management is weaker for fractional orders, and stops can be too wide or stale.

Build:
- Position Manager loop that checks each open position intraday.
- Trailing stops based on ATR, breakeven promotion after +1R, partial profit-taking at +1.5R/+2R.
- Time-stop: exit if setup has not progressed after N bars.
- Emergency de-risk if broad market or sector regime flips.
- Verify every position has a live exit plan: stop, target, reason, last refresh time.

Expected impact: preserve winners longer, cut dead trades earlier, reduce giving back gains.

### P4 — Portfolio construction and concentration engine

Problem: Picsou is aggressive, but exposure allocation is still mostly proposal-by-proposal. This can accidentally over-concentrate correlated ETF/semiconductor risk or under-size the best opportunity.

Build:
- Portfolio optimizer that scores marginal value of adding each proposal.
- Correlation and sector caps, with explicit permission to concentrate when conviction is high.
- Cash redeployment logic: replace low-expectancy positions with higher-expectancy ones instead of waiting for cash.
- Exposure budget by regime: risk-on, neutral, risk-off.

Expected impact: better use of limited cash and more intentional concentration.

### P5 — Intraday scanner and execution quality

Problem: the scanner uses daily bars and latest quotes, which is coarse for entries. A good daily setup can still be badly entered.

Build:
- 5m/15m intraday confirmation for entry timing.
- Avoid buying into the first minutes of spread volatility unless a breakout rule explicitly allows it.
- Limit-price adjustment rules: chase only within a bounded spread/ATR threshold.
- Reprice or cancel stale unfilled orders.

Expected impact: reduce slippage and improve fill quality.

### P6 — Robust observability and replayable decision logs

Problem: the journal has many events but no first-class decision replay format.

Build:
- One JSON decision object per cycle: inputs, candidate scores, vetoes, final allocation, submitted order IDs.
- Lightweight dashboard/report showing current positions, risk, realized/unrealized P&L, and why each position still deserves capital.
- Alert only on material events: order submitted/rejected, stop missing, drawdown threshold, target progress.

Expected impact: easier debugging and faster improvement cycles without WhatsApp spam.

## Recommended first implementation sequence

1. Finish fork hygiene and CI: paths, names, tests, secret template.
2. Add Catalyst Agent v1 with real news/earnings data and tests.
3. Add backtest harness for current deterministic setups.
4. Replace setup_rotation with backtest plus live-fill Bayesian weighting.
5. Build Position Manager v1 for dynamic exits and fractional take-profit handling.
6. Add portfolio construction layer before order execution.

## Strategic stance

Monseignor should not merely be “Picsou but cleaner”. It should become the research-and-execution upgrade path:
- Picsou remains the running monolith.
- Monseignor becomes the experimental branch with real alpha inputs, backtests, and dynamic portfolio management.
- Once Monseignor beats Picsou on replay/backtest and paper shadow mode, migrate live Paper capital to Monseignor.
