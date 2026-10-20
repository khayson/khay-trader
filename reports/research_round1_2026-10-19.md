# Research round 1 (2026-10-19)

## Reproducibility

- git commit: c3c8b294c61ed05f9f84c9bcfd38c6e07975ea1a
- config sha256: 9a825867acd69bb593c5338f29922ff8389c4b78fae3221dc0c69f9edab4df6d
- protocol sha256: cd6153cd53efe09ed0d7cbefe5829167fd2b1f1092c945bacde1fc91771e0df2
- known gaps sha256: 700c094b96676ea6efd85374e3352e8ad1650bd0b3d23b2bbf69c60e8ea0e7fd
- seed: 42 (random-entry runs per candidate and split: 1000; bootstrap resamples: 10000)
- data sha256 BTC/USDT (1h): 304c04aff6bc61f835a5fc4f4859a3430581f5487a6b1f94f179051cc69a53a6
- data sha256 ETH/USDT (1h): 42fb79d5148f7f24d95f941c6386e2637073f6dee688133aac29729b9bb84ada
- train: [2018-01-01 00:00:00+00:00, 2023-01-01 00:00:00+00:00)
- validate: [2023-01-01 00:00:00+00:00, 2024-10-19 00:00:00+00:00)
- test: [2024-10-19 00:00:00+00:00, 2026-10-19 22:00:00+00:00)

## Protocol

Pre-registered in config/research_round1.yaml. Exactly four candidates, all long-only spot, ATR(14) x3.0 initial and ratcheting stop with intrabar fills, sizing/caps/costs as config/default.yaml (fee 0.10% + slippage 0.05% per side), warmup 5x the longest lookback in bars:

- **A**: 4h bars, EMA 20/100: enter close > fast > slow, exit close < slow (warmup 500 bars)
- **B**: 4h bars, EMA 50/200: enter close > fast > slow, exit close < slow (warmup 1000 bars)
- **C**: 1D bars, EMA 20/100: enter close > fast > slow, exit close < slow (warmup 500 bars)
- **D**: 1D bars, enter close > prior 55-bar high, exit close < prior 20-bar low (warmup 275 bars)

Advance rule: net return after 1x costs > 0 AND >= 80th percentile of random entry, on BOTH train and validate; among those, highest validate Sharpe; at most one advances. Go on test (second look, once): same two conditions plus max drawdown <= 15%.

**Multiple comparisons:** four candidates were tried. Even with no real edge, the chance that at least one clears an 80th-percentile bar on one split by luck is far higher than for a single pre-planned test, so a pass here is weaker evidence than it looks.

**Deviation from the round-1 prompt (approved by the user):** the 27 Binance-wide outages in config/known_gaps.yaml are treated as market-closed time. Features count bars across them, resampled bars are built from the hours that traded (other incomplete bars are dropped), orders signalled before an outage are cancelled, and stops are checked at the first open after it. Restarting warmup at every outage would have left C with no tradeable train days and B/D with 10-16%.

Baselines run on 1h candles over each candidate's tradeable window. Random entry reuses the candidate's per-pair holding times, shuffled and placed at random non-overlapping times, with the same sizing, through the engine's shared equity pool. Percentile = share of random runs with a lower return.

## Candidate A

4h bars, EMA 20/100: enter close > fast > slow, exit close < slow

### A / train

Tradeable from 2018-03-26 08:00:00+00:00 (warmup); window ends 2023-01-01 00:00:00+00:00.

| variant | costs | scope | return | CAGR | Sharpe | max DD | trades | win | PF | avg trade | exposure | fees | slippage |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 1x | combined | 43.33% | 7.47% | 1.19 | -6.75% | 344 | 33.14% | 1.64 | 1.40% | 5.70% | 640.01 | 320.01 |
| A | 1x | BTC/USDT | 19.66% | 3.66% | 1.01 | -5.26% | 171 | 30.99% | 1.59 | 1.14% | 3.16% | 352.13 | 176.07 |
| A | 1x | ETH/USDT | 23.66% | 4.34% | 1.07 | -4.51% | 173 | 35.26% | 1.70 | 1.64% | 2.54% | 287.88 | 143.94 |
| A | 0.5x | combined | 49.04% | 8.31% | 1.32 | -6.42% | 344 | 33.14% | 1.75 | 1.55% | 5.70% | 326.86 | 163.43 |
| A | 0.5x | BTC/USDT | 22.67% | 4.17% | 1.14 | -4.82% | 171 | 30.99% | 1.70 | 1.30% | 3.16% | 179.88 | 89.94 |
| A | 0.5x | ETH/USDT | 26.37% | 4.79% | 1.17 | -4.29% | 173 | 35.26% | 1.80 | 1.80% | 2.54% | 146.99 | 73.49 |

| comparison (1x costs, tradeable window) | return | Sharpe | max DD |
|---|---|---|---|
| **candidate A** | 43.33% | 1.19 | -6.75% |
| buy-and-hold BTC/USDT 100% | 96.33% | 0.56 | -77.20% |
| buy-and-hold ETH/USDT 100% | 130.67% | 0.66 | -90.21% |
| buy-and-hold 50/50 | 113.50% | 0.60 | -80.90% |
| buy-and-hold 50/50 @ 5.97% (exposure-matched) | 6.78% | 0.17 | -27.85% |
| random entry p5 / median / p95 (1000 runs) | -11.02% / 3.10% / 21.10% | | |

Random-entry percentile: **100.0**. Average trade 95% CI: [0.35%, 2.53%]. Trades: 344.

### A / validate

Tradeable from 2023-01-01 00:00:00+00:00 (warmup); window ends 2024-10-19 00:00:00+00:00.

| variant | costs | scope | return | CAGR | Sharpe | max DD | trades | win | PF | avg trade | exposure | fees | slippage |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 1x | combined | 13.11% | 7.09% | 1.24 | -4.78% | 127 | 33.07% | 1.69 | 1.07% | 8.34% | 255.83 | 127.92 |
| A | 1x | BTC/USDT | 8.82% | 4.81% | 1.54 | -2.22% | 65 | 32.31% | 1.98 | 1.41% | 4.63% | 134.29 | 67.15 |
| A | 1x | ETH/USDT | 4.30% | 2.37% | 0.70 | -3.76% | 62 | 33.87% | 1.43 | 0.72% | 3.71% | 121.54 | 60.77 |
| A | 0.5x | combined | 15.16% | 8.16% | 1.42 | -4.10% | 127 | 33.86% | 1.84 | 1.22% | 8.34% | 129.10 | 64.55 |
| A | 0.5x | BTC/USDT | 9.90% | 5.39% | 1.72 | -2.04% | 65 | 32.31% | 2.18 | 1.56% | 4.63% | 67.80 | 33.90 |
| A | 0.5x | ETH/USDT | 5.25% | 2.89% | 0.84 | -3.39% | 62 | 35.48% | 1.55 | 0.87% | 3.71% | 61.30 | 30.65 |

| comparison (1x costs, tradeable window) | return | Sharpe | max DD |
|---|---|---|---|
| **candidate A** | 13.11% | 1.24 | -4.78% |
| buy-and-hold BTC/USDT 100% | 312.43% | 1.90 | -32.31% |
| buy-and-hold ETH/USDT 100% | 120.23% | 1.08 | -46.66% |
| buy-and-hold 50/50 | 216.33% | 1.57 | -37.81% |
| buy-and-hold 50/50 @ 8.34% (exposure-matched) | 18.04% | 1.08 | -9.93% |
| random entry p5 / median / p95 (1000 runs) | -0.61% / 7.11% / 15.63% | | |

Random-entry percentile: **86.5**. Average trade 95% CI: [-0.09%, 2.35%]. Trades: 127.

## Candidate B

4h bars, EMA 50/200: enter close > fast > slow, exit close < slow

### B / train

Tradeable from 2018-06-17 16:00:00+00:00 (warmup); window ends 2023-01-01 00:00:00+00:00.

| variant | costs | scope | return | CAGR | Sharpe | max DD | trades | win | PF | avg trade | exposure | fees | slippage |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| B | 1x | combined | 44.61% | 7.66% | 1.32 | -7.01% | 256 | 37.50% | 1.83 | 1.94% | 4.85% | 463.97 | 231.99 |
| B | 1x | BTC/USDT | 21.11% | 3.91% | 1.17 | -4.06% | 118 | 37.29% | 1.82 | 1.81% | 2.62% | 232.07 | 116.04 |
| B | 1x | ETH/USDT | 23.51% | 4.31% | 1.14 | -3.83% | 138 | 37.68% | 1.84 | 2.06% | 2.23% | 231.89 | 115.95 |
| B | 0.5x | combined | 48.63% | 8.25% | 1.43 | -6.79% | 256 | 37.89% | 1.93 | 2.10% | 4.85% | 235.44 | 117.72 |
| B | 0.5x | BTC/USDT | 23.07% | 4.24% | 1.26 | -3.83% | 118 | 38.14% | 1.92 | 1.96% | 2.62% | 117.71 | 58.86 |
| B | 0.5x | ETH/USDT | 25.56% | 4.66% | 1.22 | -3.72% | 138 | 37.68% | 1.94 | 2.21% | 2.23% | 117.72 | 58.86 |

| comparison (1x costs, tradeable window) | return | Sharpe | max DD |
|---|---|---|---|
| **candidate B** | 44.61% | 1.32 | -7.01% |
| buy-and-hold BTC/USDT 100% | 153.09% | 0.65 | -77.20% |
| buy-and-hold ETH/USDT 100% | 137.50% | 0.67 | -84.88% |
| buy-and-hold 50/50 | 145.30% | 0.64 | -77.52% |
| buy-and-hold 50/50 @ 5.33% (exposure-matched) | 7.75% | 0.19 | -28.06% |
| random entry p5 / median / p95 (1000 runs) | -9.28% / 5.26% / 20.66% | | |

Random-entry percentile: **100.0**. Average trade 95% CI: [0.59%, 3.40%]. Trades: 256.

### B / validate

Tradeable from 2023-01-01 00:00:00+00:00 (warmup); window ends 2024-10-19 00:00:00+00:00.

| variant | costs | scope | return | CAGR | Sharpe | max DD | trades | win | PF | avg trade | exposure | fees | slippage |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| B | 1x | combined | 9.55% | 5.20% | 0.93 | -6.05% | 117 | 36.75% | 1.50 | 0.91% | 8.26% | 232.24 | 116.12 |
| B | 1x | BTC/USDT | 7.26% | 3.97% | 1.24 | -3.34% | 62 | 40.32% | 1.71 | 1.28% | 4.60% | 126.00 | 63.00 |
| B | 1x | ETH/USDT | 2.29% | 1.27% | 0.41 | -3.26% | 55 | 32.73% | 1.26 | 0.48% | 3.66% | 106.24 | 53.12 |
| B | 0.5x | combined | 11.35% | 6.16% | 1.10 | -5.65% | 117 | 38.46% | 1.63 | 1.06% | 8.26% | 117.09 | 58.54 |
| B | 0.5x | BTC/USDT | 8.26% | 4.51% | 1.41 | -3.08% | 62 | 41.94% | 1.85 | 1.43% | 4.60% | 63.55 | 31.77 |
| B | 0.5x | ETH/USDT | 3.09% | 1.71% | 0.54 | -3.12% | 55 | 34.55% | 1.37 | 0.63% | 3.66% | 53.54 | 26.77 |

| comparison (1x costs, tradeable window) | return | Sharpe | max DD |
|---|---|---|---|
| **candidate B** | 9.55% | 0.93 | -6.05% |
| buy-and-hold BTC/USDT 100% | 312.43% | 1.90 | -32.31% |
| buy-and-hold ETH/USDT 100% | 120.23% | 1.08 | -46.66% |
| buy-and-hold 50/50 | 216.33% | 1.57 | -37.81% |
| buy-and-hold 50/50 @ 8.26% (exposure-matched) | 17.87% | 1.08 | -9.85% |
| random entry p5 / median / p95 (1000 runs) | -0.42% / 7.47% / 16.35% | | |

Random-entry percentile: **67.6**. Average trade 95% CI: [-0.30%, 2.24%]. Trades: 117.

## Candidate C

1D bars, EMA 20/100: enter close > fast > slow, exit close < slow

### C / train

Tradeable from 2019-05-15 00:00:00+00:00 (warmup); window ends 2023-01-01 00:00:00+00:00.

| variant | costs | scope | return | CAGR | Sharpe | max DD | trades | win | PF | avg trade | exposure | fees | slippage |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| C | 1x | combined | 11.06% | 2.12% | 0.75 | -3.46% | 46 | 43.48% | 2.26 | 8.81% | 1.93% | 27.67 | 13.83 |
| C | 1x | BTC/USDT | 5.71% | 1.12% | 0.75 | -2.20% | 21 | 42.86% | 2.74 | 8.94% | 1.06% | 13.95 | 6.98 |
| C | 1x | ETH/USDT | 5.35% | 1.05% | 0.61 | -2.86% | 25 | 44.00% | 1.97 | 8.69% | 0.87% | 13.71 | 6.86 |
| C | 0.5x | combined | 11.28% | 2.16% | 0.77 | -3.43% | 46 | 43.48% | 2.30 | 8.97% | 1.93% | 13.85 | 6.92 |
| C | 0.5x | BTC/USDT | 5.82% | 1.14% | 0.77 | -2.18% | 21 | 42.86% | 2.80 | 9.10% | 1.06% | 6.98 | 3.49 |
| C | 0.5x | ETH/USDT | 5.46% | 1.07% | 0.62 | -2.83% | 25 | 44.00% | 2.00 | 8.85% | 0.87% | 6.87 | 3.43 |

| comparison (1x costs, tradeable window) | return | Sharpe | max DD |
|---|---|---|---|
| **candidate C** | 11.06% | 0.75 | -3.46% |
| buy-and-hold BTC/USDT 100% | 107.58% | 0.64 | -77.20% |
| buy-and-hold ETH/USDT 100% | 451.08% | 0.97 | -81.34% |
| buy-and-hold 50/50 | 279.33% | 0.86 | -79.30% |
| buy-and-hold 50/50 @ 2.65% (exposure-matched) | 7.41% | 0.23 | -23.53% |
| random entry p5 / median / p95 (1000 runs) | -0.77% / 6.03% / 13.88% | | |

Random-entry percentile: **87.0**. Average trade 95% CI: [0.52%, 17.70%]. Trades: 46.

### C / validate

Tradeable from 2023-01-01 00:00:00+00:00 (warmup); window ends 2024-10-19 00:00:00+00:00.

| variant | costs | scope | return | CAGR | Sharpe | max DD | trades | win | PF | avg trade | exposure | fees | slippage |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| C | 1x | combined | 1.99% | 1.10% | 0.31 | -4.38% | 34 | 32.35% | 1.29 | 0.64% | 5.54% | 30.61 | 15.30 |
| C | 1x | BTC/USDT | 2.26% | 1.25% | 0.55 | -2.20% | 18 | 38.89% | 1.62 | 2.09% | 3.30% | 17.45 | 8.73 |
| C | 1x | ETH/USDT | -0.26% | -0.15% | -0.07 | -2.36% | 16 | 25.00% | 0.92 | -0.99% | 2.25% | 13.15 | 6.58 |
| C | 0.5x | combined | 2.22% | 1.23% | 0.35 | -4.30% | 34 | 32.35% | 1.33 | 0.79% | 5.54% | 15.32 | 7.66 |
| C | 0.5x | BTC/USDT | 2.39% | 1.32% | 0.58 | -2.15% | 18 | 38.89% | 1.67 | 2.24% | 3.30% | 8.74 | 4.37 |
| C | 0.5x | ETH/USDT | -0.17% | -0.09% | -0.04 | -2.32% | 16 | 25.00% | 0.95 | -0.84% | 2.25% | 6.58 | 3.29 |

| comparison (1x costs, tradeable window) | return | Sharpe | max DD |
|---|---|---|---|
| **candidate C** | 1.99% | 0.31 | -4.38% |
| buy-and-hold BTC/USDT 100% | 312.43% | 1.90 | -32.31% |
| buy-and-hold ETH/USDT 100% | 120.23% | 1.08 | -46.66% |
| buy-and-hold 50/50 | 216.33% | 1.57 | -37.81% |
| buy-and-hold 50/50 @ 5.54% (exposure-matched) | 11.99% | 1.05 | -7.06% |
| random entry p5 / median / p95 (1000 runs) | 2.61% / 6.90% / 11.99% | | |

Random-entry percentile: **2.9**. Average trade 95% CI: [-3.93%, 6.07%]. Trades: 34.

## Candidate D

1D bars, enter close > prior 55-bar high, exit close < prior 20-bar low

### D / train

Tradeable from 2018-10-02 00:00:00+00:00 (warmup); window ends 2023-01-01 00:00:00+00:00.

| variant | costs | scope | return | CAGR | Sharpe | max DD | trades | win | PF | avg trade | exposure | fees | slippage |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| D | 1x | combined | 10.96% | 2.10% | 0.78 | -2.77% | 29 | 58.62% | 3.82 | 10.40% | 1.53% | 21.99 | 10.99 |
| D | 1x | BTC/USDT | 5.96% | 1.17% | 0.73 | -1.80% | 14 | 64.29% | 3.51 | 9.77% | 0.93% | 12.07 | 6.04 |
| D | 1x | ETH/USDT | 5.00% | 0.98% | 0.62 | -1.78% | 15 | 53.33% | 4.30 | 10.99% | 0.60% | 9.92 | 4.96 |
| D | 0.5x | combined | 11.13% | 2.13% | 0.79 | -2.76% | 29 | 58.62% | 3.91 | 10.57% | 1.53% | 11.00 | 5.50 |
| D | 0.5x | BTC/USDT | 6.06% | 1.18% | 0.74 | -1.78% | 14 | 64.29% | 3.58 | 9.93% | 0.93% | 6.04 | 3.02 |
| D | 0.5x | ETH/USDT | 5.08% | 1.00% | 0.63 | -1.77% | 15 | 53.33% | 4.42 | 11.16% | 0.60% | 4.96 | 2.48 |

| comparison (1x costs, tradeable window) | return | Sharpe | max DD |
|---|---|---|---|
| **candidate D** | 10.96% | 0.78 | -2.77% |
| buy-and-hold BTC/USDT 100% | 149.51% | 0.66 | -77.20% |
| buy-and-hold ETH/USDT 100% | 414.87% | 0.88 | -81.34% |
| buy-and-hold 50/50 | 282.19% | 0.80 | -78.91% |
| buy-and-hold 50/50 @ 1.80% (exposure-matched) | 5.08% | 0.19 | -17.58% |
| random entry p5 / median / p95 (1000 runs) | -1.98% / 4.32% / 12.12% | | |

Random-entry percentile: **91.7**. Average trade 95% CI: [3.00%, 18.29%]. Trades: 29.
**Only 29 trades: too few to conclude anything about edge.**

### D / validate

Tradeable from 2023-01-01 00:00:00+00:00 (warmup); window ends 2024-10-19 00:00:00+00:00.

| variant | costs | scope | return | CAGR | Sharpe | max DD | trades | win | PF | avg trade | exposure | fees | slippage |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| D | 1x | combined | 2.57% | 1.42% | 0.46 | -2.71% | 17 | 52.94% | 1.83 | 1.69% | 3.36% | 16.89 | 8.44 |
| D | 1x | BTC/USDT | 2.57% | 1.42% | 0.77 | -1.56% | 8 | 62.50% | 3.30 | 4.80% | 1.92% | 8.56 | 4.28 |
| D | 1x | ETH/USDT | -0.00% | -0.00% | 0.01 | -1.55% | 9 | 44.44% | 1.00 | -1.08% | 1.45% | 8.33 | 4.17 |
| D | 0.5x | combined | 2.70% | 1.49% | 0.49 | -2.69% | 17 | 52.94% | 1.89 | 1.84% | 3.36% | 8.45 | 4.22 |
| D | 0.5x | BTC/USDT | 2.64% | 1.46% | 0.79 | -1.54% | 8 | 62.50% | 3.40 | 4.96% | 1.92% | 4.28 | 2.14 |
| D | 0.5x | ETH/USDT | 0.06% | 0.03% | 0.03 | -1.53% | 9 | 44.44% | 1.03 | -0.93% | 1.45% | 4.17 | 2.08 |

| comparison (1x costs, tradeable window) | return | Sharpe | max DD |
|---|---|---|---|
| **candidate D** | 2.57% | 0.46 | -2.71% |
| buy-and-hold BTC/USDT 100% | 312.43% | 1.90 | -32.31% |
| buy-and-hold ETH/USDT 100% | 120.23% | 1.08 | -46.66% |
| buy-and-hold 50/50 | 216.33% | 1.57 | -37.81% |
| buy-and-hold 50/50 @ 3.36% (exposure-matched) | 7.27% | 1.02 | -4.53% |
| random entry p5 / median / p95 (1000 runs) | -0.28% / 3.70% / 8.29% | | |

Random-entry percentile: **32.8**. Average trade 95% CI: [-3.90%, 7.57%]. Trades: 17.
**Only 17 trades: too few to conclude anything about edge.**

## Reference: Phase 3 baseline (1h, EMA 50/200, ATR% band)

| variant | costs | scope | return | CAGR | Sharpe | max DD | trades | win | PF | avg trade | exposure | fees | slippage |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1h baseline | 1x train | combined | -9.34% | -1.94% | -0.23 | -14.52% | 2081 | 20.47% | 0.95 | -0.05% | 6.52% | 3932.20 | 1966.10 |
| 1h baseline | 1x validate | combined | -15.11% | -8.70% | -1.60 | -19.45% | 868 | 18.66% | 0.73 | -0.19% | 7.26% | 1608.72 | 804.36 |

## Decision

| candidate | train net | train pct | validate net | validate pct | validate Sharpe | passes |
|---|---|---|---|---|---|---|
| A | 43.33% | 100.0 | 13.11% | 86.5 | 1.24 | yes |
| B | 44.61% | 100.0 | 9.55% | 67.6 | 0.93 | no |
| C | 11.06% | 87.0 | 1.99% | 2.9 | 0.31 | no |
| D | 10.96% | 91.7 | 2.57% | 32.8 | 0.46 | no |

## Test (second look): candidate A

This period was already used once by the Phase 3 1h baseline.

### A / test

Tradeable from 2024-10-19 00:00:00+00:00 (warmup); window ends 2026-10-19 22:00:00+00:00.

| variant | costs | scope | return | CAGR | Sharpe | max DD | trades | win | PF | avg trade | exposure | fees | slippage |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 1x | combined | 8.87% | 4.34% | 0.83 | -6.59% | 138 | 36.96% | 1.40 | 0.58% | 7.26% | 275.42 | 137.71 |
| A | 1x | BTC/USDT | 3.12% | 1.55% | 0.59 | -3.69% | 69 | 36.23% | 1.31 | 0.41% | 3.88% | 143.80 | 71.90 |
| A | 1x | ETH/USDT | 5.76% | 2.84% | 0.78 | -4.06% | 69 | 37.68% | 1.47 | 0.75% | 3.39% | 131.62 | 65.81 |
| A | 0.5x | combined | 10.98% | 5.35% | 1.01 | -5.71% | 138 | 36.96% | 1.52 | 0.73% | 7.26% | 139.06 | 69.53 |
| A | 0.5x | BTC/USDT | 4.20% | 2.08% | 0.78 | -3.26% | 69 | 36.23% | 1.45 | 0.56% | 3.88% | 72.60 | 36.30 |
| A | 0.5x | ETH/USDT | 6.78% | 3.33% | 0.92 | -3.56% | 69 | 37.68% | 1.57 | 0.90% | 3.39% | 66.45 | 33.23 |

| comparison (1x costs, tradeable window) | return | Sharpe | max DD |
|---|---|---|---|
| **candidate A** | 8.87% | 0.83 | -6.59% |
| buy-and-hold BTC/USDT 100% | 19.54% | 0.42 | -53.74% |
| buy-and-hold ETH/USDT 100% | -9.34% | 0.26 | -69.15% |
| buy-and-hold 50/50 | 5.10% | 0.31 | -60.47% |
| buy-and-hold 50/50 @ 7.26% (exposure-matched) | 0.37% | 0.06 | -7.51% |
| random entry p5 / median / p95 (1000 runs) | -9.53% / -1.25% / 6.67% | | |

Random-entry percentile: **98.3**. Average trade 95% CI: [-0.55%, 1.86%]. Trades: 138.

**GO**: net 8.87%, percentile 98.3, max drawdown -6.59%.
