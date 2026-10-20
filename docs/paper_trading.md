# Paper trading strategy v1

Paper only: public market data, no API keys, no orders reach an exchange. The 12-week
paper gate (SPEC section 9) is judged on process, so the run must be continuous.

## Machine

Run it on an **always-on machine with time sync** (a small VPS or a desktop that never
sleeps), not a laptop. A sleeping machine misses bars; more than 2 missed wakes in 7 days
blocks entries. Paper needs no keys, so a cheap VPS is safe.

- Time sync must be on (Windows: Settings > Time & language > Date & time > "Set time
  automatically", or `w32tm /resync`; Linux: `timedatectl set-ntp true`). Each wake compares
  the local clock with the exchange's; more than 2 s apart skips entries and alerts.
- Disable sleep/hibernation; keep the network up.

## Before the first run

1. Set `paper.initial_equity` in `config/default.yaml` (or env `TRADER_PAPER_EQUITY`) to
   **the amount you would live-trade and can lose entirely**. Positions are ~10% of it, so
   exchange lot sizes and minimum order values are only exercised at the real scale.
   It is read once, when the journal is created.
2. Alerts (optional but strongly recommended), as environment variables, never in files
   that are committed:
   - `TRADER_ALERT_WEBHOOK`: URL receiving a POST per alert (Slack/Discord style JSON with
     `text`/`content`). For phone push via ntfy.sh, also set `TRADER_ALERT_FORMAT=ntfy`.
   - `TRADER_DEADMAN_URL`: pinged every minute (each tick and each wake). Use e.g. a
     healthchecks.io check with **period 5 minutes, grace 10 minutes**: it alerts you when
     pings stop (dead machine, dead loop, network down) within about 15 minutes.
   Alerts go out on: kill-switch trip, missed-wake threshold, clock skew, new exchange
   outage, persistent bar-parity mismatch, divergence (explained or not), slippage above
   3x the model, stale heartbeat (via the status check), and a daily summary.
3. Commit everything: `trader-run` refuses to start if tracked files have uncommitted
   changes, and logs the commit and config hash so every paper result maps to code.

```powershell
cd C:\Users\KHAYSON\Desktop\TradingBot
.venv\Scripts\trader-backfill --since 2018-01-01      # candle history (idempotent)
.venv\Scripts\trader-killswitch rearm --reason "start paper v1" --equity <initial_equity>
.venv\Scripts\trader-run --once                        # one wake now, to check everything works
.venv\Scripts\trader-status
```

The kill switch starts tripped on a fresh install (no state file = tripped). Nothing enters
until you re-arm it with a reason; the re-arm goes to `data/state/killswitch.audit.jsonl`.

## Keep it running

`trader-run` without `--once` wakes 10 s after each 4h bar close (00:00, 04:00, ... UTC)
and ticks every minute in between (simulated stop checks, prompt flatten after a trip,
heartbeat). Only one instance can run (`data/paper/trader.lock`); a second exits with an
error, so starting it on a schedule is safe.

Windows (Task Scheduler):

1. Create Task; General: "Run whether user is logged on or not".
2. Triggers: "At startup", plus "Daily" repeating every 1 hour (restarts it if it died).
3. Action: Program `C:\Users\KHAYSON\Desktop\TradingBot\.venv\Scripts\trader-run.exe`,
   Start in `C:\Users\KHAYSON\Desktop\TradingBot`. Set the alert environment variables for
   that user (System Properties > Environment Variables).
4. Settings: "If the task is already running: Do not start a new instance"; untick "Stop
   the task if it runs longer than". Conditions: untick the AC-power option.

Linux: a systemd service with `Restart=always`, `WorkingDirectory` set to the repo and the
alert variables in an `EnvironmentFile` with mode 600.

## Stale-heartbeat check

```powershell
.venv\Scripts\trader-status --check-heartbeat
```

Exits 1 and sends an alert if the heartbeat is older than 10 minutes (the loop beats every
minute). Schedule it every 15 minutes as a second line of defence next to the dead-man ping.

## How the loop handles trouble

- Exchange/API errors: every direct call retries 3 times over ~30 s. A wake that still
  fails is retried every minute and counts once per bar toward the kill switch (5 in a row
  trips). Diagnostics (parity sweep, divergence, summary) run after trading and can never
  block or undo it.
- Crashes (bugs): logged with a stack trace; 5 in a row trips the kill switch with reason
  "5 consecutive crashes".
- Missing hours the exchange never returns are confirmed with a refetch, recorded as
  outages in the journal (market-closed time, like `config/known_gaps.yaml`) and alerted.
  Entries are skipped for a bar that contains one; exits still run.
- Every expected wake slot is journaled as processed, late, missed, stale (feed behind) or
  outage. Late or stale wakes still execute exits; entries need fresh data.
- Kill switch tripped (manual, drawdown, API errors, crashes or unexplained divergence):
  entries stop; with `flatten_on_trip` each position is market-sold within a minute and its
  stop cancelled only after the sell fills.

## Divergence check (once a UTC day)

The loop replays the backtest engine over the same bars since paper start and compares
entries and exits bar by bar. A mismatch is **explained** (logged, alerted, counted, no
trip) when it follows a missed/late/stale wake, a runtime outage, a data revision (the
replay's features for a decision bar differ from those recorded at decision time) or an
entry the loop declined for an operational reason (clock, parity, exchange minimum,
missed-wake block, kill switch), within the same position episode. Any **unexplained**
mismatch trips the kill switch and flattens.

It also reports fill slippage: total versus the 1h-open reference (what the 0.05% model
represents), split into impact versus the book mid at fill time and drift of the mid from
the open. It warns when the total exceeds 3x the model over at least 10 fills.

## What to look at

- `trader-status`: heartbeat, kill switch, cash, positions with stops, missed wakes,
  recent decisions and events.
- `data/paper/paper.db` (SQLite): decisions (with features), fills (price, modeled price,
  mid, spread), trades, equity, wakes, outages, parity, events.
- `data/paper/logs/trader.jsonl`: rotating structured logs.
