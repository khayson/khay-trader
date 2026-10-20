# Paper trading strategy v1 on Windows

Paper only: public market data, no API keys, no orders reach an exchange.

## First run

```powershell
cd C:\Users\KHAYSON\Desktop\TradingBot
.venv\Scripts\trader-backfill --since 2018-01-01      # candle history (idempotent)
.venv\Scripts\trader-killswitch rearm --reason "start paper v1" --equity 10000
.venv\Scripts\trader-run --once                        # one wake now, to check everything works
.venv\Scripts\trader-status
```

The kill switch starts tripped on a fresh install (no state file = tripped). Nothing enters
until you re-arm it with a reason; the re-arm is written to
`data/state/killswitch.audit.jsonl`.

## Keep it running (Task Scheduler)

`trader-run` without `--once` runs forever, waking about 5 seconds after each 4h bar close
(00:00, 04:00, ... UTC). Only one instance can run at a time (`data/paper/trader.lock`); a
second one exits with an error, so it is safe to start it on a schedule.

1. Task Scheduler > Create Task.
2. General: "Run whether user is logged on or not"; "Do not store password" is fine.
3. Triggers: "At startup", plus "Daily" repeating every 1 hour (restarts it if it died).
4. Actions: Start a program
   - Program: `C:\Users\KHAYSON\Desktop\TradingBot\.venv\Scripts\trader-run.exe`
   - Start in: `C:\Users\KHAYSON\Desktop\TradingBot`
5. Settings: "If the task is already running: Do not start a new instance";
   untick "Stop the task if it runs longer than".
6. Power: untick "Start the task only if the computer is on AC power"; disable sleep, or
   the loop misses bars (exits still run on the next wake; entries on stale bars are skipped).

## Stale-heartbeat check

```powershell
.venv\Scripts\trader-status --check-heartbeat
```

Exits 1 if the heartbeat is older than one bar plus 10 minutes. Schedule it hourly in Task
Scheduler and have it alert you (for example a "Send an e-mail"/notification action on a
non-zero exit), or just run it when you check in.

## What to look at

- `trader-status`: heartbeat, kill switch, cash, positions with stops, recent decisions and
  events (API errors, stale feed, parity mismatches, divergence, slippage).
- `data/paper/paper.db` (SQLite): decisions, fills (actual vs modeled price), trades,
  equity, parity, events. Open with any SQLite browser.
- `data/paper/logs/trader.jsonl`: rotating structured logs.

Once a UTC day the loop replays the backtest engine over the same bars since paper start.
Any difference in entries or exits trips the kill switch (`divergence` in the reason). It
also reports average market-fill slippage against the 0.05% model and warns above 3x.
