# Stop-fill slippage sensitivity: strategy v1 (candidate A)

**Sensitivity check, not a new test.** Same candles, splits, parameters and engine as research round 1; only the slippage on stop fills changes. Entries and signal exits stay at 0.05%; fees stay at 0.10% per side. Test was already viewed (round 1 second look), so nothing here is out-of-sample evidence.

- git commit: bd45877bcf99a04322f5cd1447525bb9f3cca46f
- config sha256: 9a825867acd69bb593c5338f29922ff8389c4b78fae3221dc0c69f9edab4df6d
- protocol sha256: 2e6eb875fa0ab468f02a34f130102eb6d9be7863468701b12225cc0089b3987a
- known gaps sha256: 700c094b96676ea6efd85374e3352e8ad1650bd0b3d23b2bbf69c60e8ea0e7fd
- data sha256 BTC/USDT (1h): 304c04aff6bc61f835a5fc4f4859a3430581f5487a6b1f94f179051cc69a53a6
- data sha256 ETH/USDT (1h): 42fb79d5148f7f24d95f941c6386e2637073f6dee688133aac29729b9bb84ada

| split | stop slippage | return | Sharpe | max DD | trades | stop exits |
|---|---|---|---|---|---|---|
| train | 1x (0.05%) | 43.33% | 1.19 | -6.75% | 344 | 226 |
| train | 3x (0.15%) | 40.89% | 1.13 | -6.88% | 344 | 226 |
| train | 6x (0.30%) | 37.31% | 1.05 | -7.06% | 344 | 226 |
| validate | 1x (0.05%) | 13.11% | 1.24 | -4.78% | 127 | 83 |
| validate | 3x (0.15%) | 12.23% | 1.15 | -5.08% | 127 | 83 |
| validate | 6x (0.30%) | 10.92% | 1.03 | -5.53% | 127 | 83 |
| test | 1x (0.05%) | 8.87% | 0.83 | -6.59% | 138 | 106 |
| test | 3x (0.15%) | 7.80% | 0.73 | -7.02% | 138 | 106 |
| test | 6x (0.30%) | 6.21% | 0.59 | -7.67% | 138 | 106 |
