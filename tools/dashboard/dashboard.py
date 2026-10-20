"""Read-only dashboard for the paper loop's SQLite journal.

A separate tool: it never imports the loop, opens the journal with SQLite's read-only
mode (mode=ro), serves on 127.0.0.1 only, answers GET and nothing else, and has no
controls. Reach it on a server through an SSH tunnel, e.g.
`ssh -L 8765:127.0.0.1:8765 <server>`, then open http://127.0.0.1:8765.

Usage (from the repo root):
  .venv/Scripts/python tools/dashboard/dashboard.py              # serve on 127.0.0.1:8765
  .venv/Scripts/python tools/dashboard/dashboard.py --html out.html   # write a static page
"""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from trader.exec.divergence import slippage_check
from trader.risk import killswitch as ks

HOST = "127.0.0.1"  # never configurable: the dashboard is local-only
TIME_COLUMNS = (
    "time",
    "signal_time",
    "entry_time",
    "exit_time",
    "wake_time",
    "bar_close",
    "bar_open",
    "checked_at",
    "slot",
)


def open_readonly(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise FileNotFoundError(f"no journal at {db}")
    return sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)


# Columns the page reads, so a journal written by an older loop version (missing a table
# the dashboard cannot create, being read-only) still renders with that part empty.
COLUMNS = {
    "equity": ["time", "equity", "cash", "exposure"],
    "positions": ["symbol", "qty", "entry_price", "entry_time", "entry_cost", "stop_price"],
    "decisions": ["id", "bar_close", "symbol", "features", "action", "risk"],
    "events": ["id", "time", "kind", "message"],
    "wakes": ["slot", "status", "wake_time"],
    "fills": ["id", "time", "symbol", "side", "qty", "price", "raw_price", "reason"],
}


def _table_frame(conn: sqlite3.Connection, table: str, tail: str) -> pd.DataFrame:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if not exists:
        return pd.DataFrame(columns=COLUMNS[table])
    return _frame(conn, f"SELECT * FROM {table} {tail}")


def _frame(conn: sqlite3.Connection, sql: str) -> pd.DataFrame:
    df = pd.read_sql_query(sql, conn)
    for col in TIME_COLUMNS:
        if col in df:
            df[col] = pd.to_datetime(df[col], utc=True, format="ISO8601")
    return df


@dataclass(frozen=True)
class Snapshot:
    now: pd.Timestamp
    kv: dict[str, str]
    killswitch: ks.State
    equity: pd.DataFrame
    positions: pd.DataFrame
    decisions: pd.DataFrame
    events: pd.DataFrame
    summaries: pd.DataFrame
    wakes: pd.DataFrame
    fills: pd.DataFrame
    model_slippage: float


def load(db: Path, ks_path: Path, model_slippage: float, now: pd.Timestamp) -> Snapshot:
    conn = open_readonly(db)
    try:
        kv = dict(conn.execute("SELECT key, value FROM kv").fetchall())
        events = _table_frame(conn, "events", "ORDER BY id DESC LIMIT 200")
        return Snapshot(
            now=now,
            kv={str(k): str(v) for k, v in kv.items()},
            killswitch=ks.load(ks_path),
            equity=_table_frame(conn, "equity", "ORDER BY time"),
            positions=_table_frame(conn, "positions", "ORDER BY symbol"),
            decisions=_table_frame(conn, "decisions", "ORDER BY id DESC LIMIT 50"),
            events=events[events["kind"] != "summary"].head(50),
            summaries=events[events["kind"] == "summary"].head(14),
            wakes=_table_frame(conn, "wakes", "ORDER BY slot"),
            fills=_table_frame(conn, "fills", "ORDER BY id"),
            model_slippage=model_slippage,
        )
    finally:
        conn.close()


# --- rendering -------------------------------------------------------------------------

CSS = """
:root { --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --series:#2a78d6; --border:rgba(11,11,11,0.10);
  --good:#006300; --critical:#d03b3b; }
@media (prefers-color-scheme: dark) { :root { --page:#0d0d0d; --surface:#1a1a19;
  --ink:#f0efec; --ink2:#c3c2b7; --grid:#2c2c2a; --axis:#383835; --series:#3987e5;
  --border:rgba(255,255,255,0.10); --good:#0ca30c; --critical:#e66767; } }
* { box-sizing: border-box; }
body { margin:0; padding:16px; background:var(--page); color:var(--ink);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1100px; margin:0 auto; }
h1 { font-size:20px; margin:0 0 4px; } h2 { font-size:15px; margin:0 0 8px; }
.sub { color:var(--ink2); margin:0 0 16px; }
section { background:var(--surface); border:1px solid var(--border); border-radius:8px;
  padding:16px; margin:0 0 16px; overflow-x:auto; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; }
.tile .label { color:var(--ink2); font-size:12px; } .tile .value { font-size:22px; }
.ok { color:var(--good); } .bad { color:var(--critical); font-weight:600; }
table { border-collapse:collapse; width:100%; font-size:13px; }
th, td { text-align:left; padding:4px 8px; border-bottom:1px solid var(--grid);
  vertical-align:top; }
th { color:var(--ink2); font-weight:600; }
th.num { text-align:right; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
.feat { color:var(--ink2); font-size:12px; max-width:420px; }
.chart { position:relative; } svg { display:block; width:100%; height:auto; }
.tip { position:absolute; pointer-events:none; background:var(--surface); color:var(--ink);
  border:1px solid var(--border); border-radius:6px; padding:4px 8px; font-size:12px;
  display:none; white-space:nowrap; }
details summary { cursor:pointer; color:var(--ink2); margin-top:8px; }
"""

CHART_JS = """
(() => {
  const data = JSON.parse(document.getElementById('equity-data').textContent);
  const svg = document.getElementById('equity'); const tip = document.getElementById('tip');
  const line = document.getElementById('cross'); const dot = document.getElementById('dot');
  if (!svg || !data.x.length) return;
  svg.addEventListener('mousemove', (ev) => {
    const box = svg.getBoundingClientRect();
    const sx = (ev.clientX - box.left) * (data.w / box.width);
    let i = 0, best = Infinity;
    data.x.forEach((x, k) => { const d = Math.abs(x - sx); if (d < best) { best = d; i = k; } });
    line.setAttribute('x1', data.x[i]); line.setAttribute('x2', data.x[i]);
    line.style.display = 'block'; dot.style.display = 'block';
    dot.setAttribute('cx', data.x[i]); dot.setAttribute('cy', data.y[i]);
    tip.textContent = data.t[i] + '  equity ' + data.v[i];
    tip.style.display = 'block';
    tip.style.left = Math.min(box.width - 180, (data.x[i] / data.w) * box.width + 8) + 'px';
    tip.style.top = ((data.y[i] / data.h) * box.height - 28) + 'px';
  });
  svg.addEventListener('mouseleave', () => {
    tip.style.display = 'none'; line.style.display = 'none'; dot.style.display = 'none';
  });
})();
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None or value != value else str(value))


def _fmt_time(value: Any) -> str:
    if value is None or value is pd.NaT or value != value:
        return ""
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")


def _table(df: pd.DataFrame, columns: list[tuple[str, str, Callable[[Any], str]]]) -> str:
    if df.empty:
        return "<p class='sub'>Nothing yet.</p>"
    head = "".join(
        f"<th{' class=num' if fmt in (_money, _pct) else ''}>{_e(title)}</th>"
        for _, title, fmt in columns
    )
    rows = []
    for _, row in df.iterrows():
        cells = []
        for col, _, fmt in columns:
            text = fmt(row[col]) if col in row else ""
            cls = " class='num'" if fmt in (_money, _pct) else ""
            cells.append(f"<td{cls}>{text}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _age(age: pd.Timedelta | None) -> str:
    if age is None:
        return "never"
    minutes = int(age.total_seconds() // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h {minutes % 60} min"


def _money(v: Any) -> str:
    return "" if v is None or v != v else f"{float(v):,.2f}"


def _pct(v: Any) -> str:
    return "n/a" if v is None or v != v else f"{float(v):.2%}"


def _features(v: Any) -> str:
    try:
        values = json.loads(v) if isinstance(v, str) else None
    except json.JSONDecodeError:
        values = None
    if not values:
        return ""
    keys = ("close", "ema_fast", "ema_slow", "atr")
    text = ", ".join(f"{k} {values[k]:,.2f}" for k in keys if k in values)
    return f"<span class='feat'>{_e(text)}</span>"


def equity_chart(equity: pd.DataFrame, start: float) -> str:
    """One series, so no legend: the heading names it. Hover crosshair + tooltip; a table
    view sits below for keyboard/screen-reader use."""
    if equity.empty:
        return "<p class='sub'>No equity snapshots yet.</p>"
    w, h, left, right, top, bottom = 1000, 260, 70, 16, 12, 30
    t = pd.DatetimeIndex(equity["time"])
    v: list[float] = [float(x) for x in equity["equity"]]
    lo, hi = min(*v, start), max(*v, start)
    pad = (hi - lo) * 0.08 or max(abs(hi) * 0.01, 1.0)
    lo, hi = lo - pad, hi + pad
    span = (t[-1] - t[0]).total_seconds() or 1.0

    def sx(ts: pd.Timestamp) -> float:
        return left + (ts - t[0]).total_seconds() / span * (w - left - right)

    def sy(val: float) -> float:
        return top + (hi - val) / (hi - lo) * (h - top - bottom)

    xs = [sx(ts) for ts in t]
    ys = [sy(val) for val in v]
    grid, labels = [], []
    for k in range(5):
        val = lo + (hi - lo) * k / 4
        y = sy(val)
        grid.append(
            f"<line x1='{left}' x2='{w - right}' y1='{y:.1f}' y2='{y:.1f}' "
            "stroke='var(--grid)' stroke-width='1'/>"
        )
        labels.append(
            f"<text x='{left - 8}' y='{y + 4:.1f}' text-anchor='end' "
            f"fill='var(--muted)' font-size='11'>{val:,.0f}</text>"
        )
    for ts in (t[0], t[len(t) // 2], t[-1]) if len(t) > 2 else (t[0], t[-1]):
        anchor = "start" if ts == t[0] else ("end" if ts == t[-1] else "middle")
        labels.append(
            f"<text x='{sx(ts):.1f}' y='{h - 8}' text-anchor='{anchor}' "
            f"fill='var(--muted)' font-size='11'>{_e(ts.strftime('%Y-%m-%d'))}</text>"
        )
    base = sy(start)
    path = " ".join(
        f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}"
        for i, (x, y) in enumerate(zip(xs, ys, strict=True))
    )
    payload = json.dumps(
        {
            "w": w,
            "h": h,
            "x": [round(x, 1) for x in xs],
            "y": [round(y, 1) for y in ys],
            "t": [ts.strftime("%Y-%m-%d %H:%M UTC") for ts in t],
            "v": [f"{val:,.2f}" for val in v],
        }
    ).replace("</", "<\\/")
    table = _table(
        equity.iloc[::-1],
        [
            ("time", "Bar close (UTC)", _fmt_time),
            ("equity", "Equity", _money),
            ("cash", "Cash", _money),
            ("exposure", "Exposure", _pct),
        ],
    )
    return f"""
<div class='chart'>
<svg id='equity' viewBox='0 0 {w} {h}' role='img'
  aria-label='Paper equity per 4h bar since paper start'>
  {"".join(grid)}
  <line x1='{left}' x2='{w - right}' y1='{base:.1f}' y2='{base:.1f}' stroke='var(--axis)'
    stroke-width='1' stroke-dasharray='4 4'/>
  <text x='{w - right}' y='{base - 6:.1f}' text-anchor='end' fill='var(--ink2)'
    font-size='11'>start {start:,.0f}</text>
  <path d='{path}' fill='none' stroke='var(--series)' stroke-width='2'
    stroke-linejoin='round' stroke-linecap='round'/>
  <line id='cross' y1='{top}' y2='{h - bottom}' stroke='var(--axis)' stroke-width='1'
    style='display:none'/>
  <circle id='dot' r='4' fill='var(--series)' stroke='var(--surface)' stroke-width='2'
    style='display:none'/>
  {"".join(labels)}
</svg>
<div id='tip' class='tip'></div>
</div>
<script type='application/json' id='equity-data'>{payload}</script>
<details><summary>Table view</summary>{table}</details>
"""


def render(s: Snapshot) -> str:
    start = float(s.kv.get("initial_equity") or s.kv.get("cash") or 0.0)
    latest = float(s.equity["equity"].iloc[-1]) if not s.equity.empty else start
    change = latest / start - 1 if start else 0.0
    state = s.killswitch
    ks_html = (
        "<span class='bad'>&#9888; TRIPPED</span>"
        if state.tripped
        else "<span class='ok'>&#10003; armed</span>"
    )
    heartbeat = pd.Timestamp(s.kv["heartbeat"]) if "heartbeat" in s.kv else None
    age = s.now - heartbeat if heartbeat is not None else None
    stale = age is None or age > pd.Timedelta(minutes=10)
    hb_html = (
        f"<span class='bad'>&#9888; {_e(_age(age))}</span>"
        if stale
        else f"<span class='ok'>&#10003; {_e(_age(age))}</span>"
    )
    week = s.now - pd.Timedelta(days=7)
    counts = s.wakes["status"].value_counts().to_dict() if not s.wakes.empty else {}
    recent = (
        s.wakes[s.wakes["slot"] >= week]["status"].value_counts().to_dict()
        if not s.wakes.empty
        else {}
    )

    def wake_cell(status: str) -> str:
        return f"{counts.get(status, 0)} total, {recent.get(status, 0)} in 7 days"

    report = slippage_check(s.fills, s.model_slippage, 10, 3.0) if not s.fills.empty else None
    slip = (
        "<p class='sub'>No market fills yet.</p>"
        if report is None
        else f"<table><tbody><tr><th>Market fills</th><td class='num'>{report.fills}</td></tr>"
        f"<tr><th>Total vs bar-open reference</th><td class='num'>{_pct(report.total)}</td></tr>"
        f"<tr><th>Impact vs book mid</th><td class='num'>{_pct(report.impact)}</td></tr>"
        f"<tr><th>Drift of mid vs open</th><td class='num'>{_pct(report.drift)}</td></tr>"
        f"<tr><th>Model</th><td class='num'>{_pct(s.model_slippage)}</td></tr>"
        f"<tr><th>Above 3x model?</th><td>{'yes' if report.warn else 'no'}</td></tr>"
        "</tbody></table>"
    )
    tiles = [
        ("Equity", f"{latest:,.2f}"),
        ("Since start", f"{change:+.2%}"),
        ("Cash", _money(s.kv.get("cash"))),
        ("Kill switch", ks_html),
        ("Heartbeat age", hb_html),
        ("Paper start", _e(_fmt_time(s.kv.get("paper_start")))),
    ]
    tiles_html = "".join(
        f"<div class='tile'><div class='label'>{_e(k)}</div><div class='value'>{v}</div></div>"
        for k, v in tiles
    )
    return f"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Paper Trader Status</title><style>{CSS}</style></head>
<body><main>
<h1>Paper trader: strategy v1</h1>
<p class='sub'>Read-only view of the journal at {_e(s.now.strftime("%Y-%m-%d %H:%M UTC"))}.
Kill switch: {_e(state.reason)}</p>
<section><div class='tiles'>{tiles_html}</div></section>
<section><h2>Equity per 4h bar</h2>{equity_chart(s.equity, start)}</section>
<section><h2>Positions and resting stops</h2>{
        _table(
            s.positions,
            [
                ("symbol", "Symbol", _e),
                ("qty", "Qty", _e),
                ("entry_price", "Entry", _money),
                ("entry_time", "Since (UTC)", _fmt_time),
                ("stop_price", "Stop", _money),
            ],
        )
    }</section>
<section><h2>Wake slots</h2><table><tbody>
<tr><th>Processed</th><td>{wake_cell("processed")}</td></tr>
<tr><th>Late</th><td>{wake_cell("late")}</td></tr>
<tr><th>Missed</th><td>{wake_cell("missed")}</td></tr>
<tr><th>Stale feed</th><td>{wake_cell("stale")}</td></tr>
<tr><th>Outage</th><td>{wake_cell("outage")}</td></tr></tbody></table></section>
<section><h2>Fill slippage</h2>{slip}</section>
<section><h2>Last 50 decisions</h2>{
        _table(
            s.decisions,
            [
                ("bar_close", "Bar close (UTC)", _fmt_time),
                ("symbol", "Symbol", _e),
                ("action", "Action", _e),
                ("risk", "Risk result", _e),
                ("features", "Features", _features),
            ],
        )
    }
</section>
<section><h2>Events</h2>{
        _table(
            s.events,
            [("time", "Time (UTC)", _fmt_time), ("kind", "Kind", _e), ("message", "Message", _e)],
        )
    }
</section>
<section><h2>Daily summaries</h2>{
        _table(s.summaries, [("time", "Time (UTC)", _fmt_time), ("message", "Summary", _e)])
    }</section>
</main><script>{CHART_JS}</script></body></html>"""


# --- serving ---------------------------------------------------------------------------


def make_server(page: Callable[[], str], port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if self.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            try:
                body = page().encode()
            except Exception as e:  # show the problem; never crash the server
                self.send_error(500, explain=type(e).__name__)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # quiet console
            pass

    return ThreadingHTTPServer((HOST, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only paper trader dashboard.")
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--html", type=Path, help="write one static page and exit")
    args = parser.parse_args(argv)
    cfg = yaml.safe_load(args.config.read_text())
    db, ks_path = Path(cfg["paper"]["db"]), Path(cfg["risk"]["killswitch_state"])
    slippage = float(cfg["slippage_per_side"])

    def page() -> str:
        return render(load(db, ks_path, slippage, pd.Timestamp.now(tz="UTC")))

    if args.html:
        args.html.write_text(page(), encoding="utf-8")
        print(f"wrote {args.html}")
        return 0
    server = make_server(page, args.port)
    print(f"dashboard on http://{HOST}:{server.server_address[1]} (read-only; Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
