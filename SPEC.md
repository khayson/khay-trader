# Khay Trader: Spec v1

Crypto spot trading bot. Paper-first. Architect/reviewer: Claude (chat). Builder: Claude Code.
Studio: Khay Studios

---

## 1. Goals and non-goals

**Goals**
- Run 24/7 on spot markets (BTC/USDT, ETH/USDT to start).
- Deterministic code owns execution, sizing, risk limits and the kill switch.
- An LLM (Claude API) is used only offline: nightly review and research notes. It never sits in the live order path.
- Every phase is tested and approved before the next begins.

**Non-goals (v1)**
- No leverage, no futures, no shorting.
- No LLM decisions per candle.
- No real money until the go-live gate (section 9) is passed.
- No third-party "agent frameworks" or hosted decision models. Plain Python and the Claude API only.

## 2. Stack

- Python 3.12, `uv` or `poetry`
- `ccxt` (exchange abstraction), `pandas`, `numpy`, `pydantic`, `pyyaml`
- Storage: SQLite (trades, orders, state) + Parquet (candles)
- `pytest`, `ruff`, `mypy`
- Optional later: FastAPI dashboard
- Claude API (Anthropic SDK) for the nightly review only

## 3. Repo layout

```
khay-trader/
  pyproject.toml
  config/default.yaml
  src/trader/
    data/fetch.py          # pull candles via ccxt (public endpoints)
    data/store.py          # parquet + sqlite helpers
    state/snapshot.py      # deterministic feature snapshot
    strategy/baseline.py   # rule-based strategy
    strategy/base.py       # Strategy interface
    backtest/engine.py     # event-driven backtester
    backtest/costs.py      # fees + slippage models
    backtest/metrics.py    # Sharpe, max DD, win rate, Brier (if probabilistic)
    risk/limits.py         # hard limits, pure functions
    risk/killswitch.py     # armed/disarmed state, persisted
    exec/base.py           # Broker interface
    exec/paper.py          # simulated fills
    exec/live.py           # ccxt live broker (disabled by default)
    loop.py                # main live/paper loop
    review/nightly.py      # daily report + Claude API call
    logging_setup.py
  tests/
  reports/                 # nightly reports (markdown)
  data/                    # candles, db (gitignored)
```

## 4. Config defaults (`config/default.yaml`)

```yaml
mode: paper                 # paper | live  (live requires explicit flag + env var)
pairs: [BTC/USDT, ETH/USDT]
timeframe: 1h
fees_per_side: 0.001        # 0.10%
slippage_per_side: 0.0005   # 0.05%
risk:
  max_drawdown_pct: 15      # from equity peak -> kill switch trips
  max_position_pct: 10      # per pair, of equity
  max_total_exposure_pct: 20
  max_daily_loss_pct: 2
  risk_per_trade_pct: 0.5
  max_orders_per_hour: 6
  stale_data_seconds: 180   # no trading on stale data
```

## 5. Strategy v1 (frozen, git tag `strategy-v1`)

Candidate A from research round 1 (`reports/research_round1_2026-10-19.md`). Frozen: any change is v2 and restarts the paper clock.

- Long-only spot, BTC/USDT and ETH/USDT, one shared equity pool.
- 4h bars, UTC-aligned, resampled from 1h candles (documented exchange outages treated as market-closed time).
- Entry: close > EMA20 > EMA100.
- Exit: close < EMA100, or the ATR(14) x3 stop. The stop is set at entry and ratchets up only at bar closes.
- Size: fixed-fractional risk (`risk_per_trade_pct`) over stop distance, capped by `max_position_pct`. The cap binds almost always, so in practice each position is about 10% of equity.
- Warmup: 500 bars (5x the longest lookback).
- Evidence (1x costs): train +43.3%, validate +13.1%, test (second look) +8.9% with max drawdown -6.6%, 86.5th-100th percentile vs random entry. Caveats: four candidates were tried, the test interval on average trade includes zero, and the test period had been seen once before. Treat as worth testing, not as a proven edge.
- The Phase 2-3 1h baseline (EMA50/200 with ATR% band) failed after costs and is kept only as a reference.
- Any later idea (including LLM-derived ones) must beat v1 after costs, out-of-sample.

## 6. Hard risk rules (code only, never delegated to a model)

1. Pre-trade check on every order: position cap, exposure cap, daily loss, order rate, data freshness.
2. Kill switch: trips on max drawdown, repeated API errors, or manual command. When tripped, cancel open orders, flatten (live) or stop (paper), and refuse new orders until manually re-armed.
3. Kill switch state persists across restarts.
4. Risk module has no dependency on the LLM or strategy modules.
5. Live mode requires `mode: live` and env var `TRADER_LIVE_CONFIRM=yes` and a tiny capital cap in config.
6. Live stops are exchange-native stop orders resting on the exchange, replaced at each bar close when the stop ratchets. Paper trading simulates the same behaviour (fill at min(open, stop) when a candle's low touches the stop), matching the backtest.

## 7. Phases

Each phase ends with tests green and my review. Do not start the next phase until I approve.

### Phase 1: Data layer
Deliver: `data/fetch.py`, `data/store.py`, a CLI to backfill 2+ years of 1h candles for the pairs, gap detection, and a test for timestamp integrity (no duplicates, no gaps, UTC).
Accept: backfill runs idempotently; gap report is clean or documented.

### Phase 2: State engine and strategy
Deliver: `state/snapshot.py` (EMA, ATR, volatility, returns, drawdown; computed only from candles closed at or before the snapshot time) and `strategy/baseline.py`.
Accept: a look-ahead test proves no feature uses future data; snapshot is deterministic for the same input.

### Phase 3: Backtester
Deliver: event-driven engine, fee and slippage models, metrics, walk-forward split (train/validate/test, never tuned on test), and a markdown report.
Accept: results shown with and without costs; out-of-sample numbers reported separately; a sanity test with a known toy series.

### Phase 4: Risk layer
Deliver: `risk/limits.py`, `risk/killswitch.py`, and unit tests for every rule including restart persistence.
Accept: forced-failure tests (stale data, drawdown breach, API error burst) all trip correctly.

### Phase 5: Paper-trading loop
Deliver: `exec/paper.py`, `loop.py`, SQLite order and trade log, crash-safe restart, heartbeat log.
Accept: runs 7 days unattended without silent failure; every order passes through risk checks; logs reconcile with equity.

### Phase 6: Nightly review
Deliver: `review/nightly.py` producing `reports/YYYY-MM-DD.md` (fills, misses, slippage vs model, drawdown, rule hit counts) and an optional Claude API summary with suggested parameter changes.
Accept: suggestions are written to the report only. Nothing is applied automatically. I approve each change.

## 8. Claude Code prompts

Run one per phase, in a fresh session with this file in the repo root.

**Phase prompt template:**
> Read SPEC.md. Implement Phase N only, test-first. Do not touch later phases. Follow the repo layout exactly. When done, run tests, ruff and mypy, and give me a short summary plus anything you were unsure about. Stop and wait for review.

**Standing rules to paste into CLAUDE.md:**
- Never add a hard limit, threshold or order side effect to model-driven code.
- Never commit API keys; use env vars and `.env` (gitignored).
- Use UTC everywhere.
- Prefer small pure functions; no global state in risk code.

## 9. Go-live gate (all must be true)

Strategy v1 trades about 1.4 times a week across both pairs, so a few weeks of paper P&L says nothing. The gate is about process.

- Backtest edge survives costs out-of-sample (done for v1, with the caveats in section 5).
- 12+ weeks of paper trading with strategy v1 unchanged, judged on process:
  - every paper signal matches a backtest replay over the same candles;
  - paper fills match the modeled fees and slippage;
  - the risk layer and kill switch behave as specified (rejections and trips logged and explained);
  - P&L only needs to avoid disaster (drawdown within limits).
- Paper fills vs live order book spot-checked for realistic slippage.
- Kill switch tested manually.
- Exchange confirmed to accept Ghanaian users; its licensing status checked against current Bank of Ghana and SEC rules.
- Live start with a capital cap set to an amount you can lose entirely. Scale up only after about 20 completed live trades behave like paper.
- Trade log export works for tax records (Ghana Revenue Authority).

## 10. What could be wrong

- Backtested edge may be overfit or just noise; most retail bots lose after costs.
- Paper fills are optimistic; live slippage and partial fills will be worse.
- Regime change can break any rule-based strategy; the drawdown kill switch is the safety net, not the strategy.
- Exchange outages, API changes and rate limits can strand open positions.
- Regulation in Ghana is still being rolled out in phases; re-check before going live.
- A system that survives beats one that looks profitable. If results are marginal, stop and rethink rather than add complexity.
