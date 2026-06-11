# gexbot — architecture & developer guide

Onboarding doc for anyone working on the code. For *install / run / config*, see
the [README](README.md); this explains *how the pieces fit and why*, the
contracts between them, the dev loop, and the gotchas.

## What it does, in one paragraph

gexbot computes SPX **gamma exposure (GEX)** from live tastytrade options data and
draws the key levels — put/call walls, gamma flip, gamma peak, spot, and the
per-strike net-GEX profile — onto a **local TradingView Desktop** chart as a Pine
indicator. It used to serve those levels as JSON to a remote website behind a
Cloudflare tunnel; that path was removed (2026-06-11). Everything now stays on the
machine.

## Runtime topology

```
                tastytrade DXLink (options Greeks + Summary)
                          │
        ┌─────────────────▼─────────────────┐
        │  gex_stream_service  (daemon)      │   holds one persistent
        │  recompute every ~12s, RTH only    │   subscription, 0DTE SPX
        └─────────────────┬─────────────────┘
                          │ writes
                  data/gex_levels_live.json   ◄── the contract between halves
                          │ reads
        ┌─────────────────▼─────────────────┐
        │  tv_publisher  (daemon)            │   render + push when levels move
        │  render_pine() → .pine             │
        └─────────────────┬─────────────────┘
                          │ shells out:  node <tradingview-mcp>/src/cli/index.js pine set|compile
        ┌─────────────────▼─────────────────┐
        │  tradingview-mcp CLI  (Node)       │   separate repo: ~/tradingview-mcp
        └─────────────────┬─────────────────┘
                          │ Chrome DevTools Protocol, localhost:9222
        ┌─────────────────▼─────────────────┐
        │  TradingView Desktop (Electron)    │   "GEXBot SPX Levels" indicator
        └────────────────────────────────────┘
```

The two daemons are decoupled by a file. `gex_stream` only knows tastytrade;
`tv_publisher` only knows the JSON + the TradingView CLI. Either can be restarted
independently. **Both must run on the machine with TradingView Desktop** (the
publisher needs the local CDP port), so in practice they run together — see the
launchd agents below.

## Components

| File | Responsibility | Key entry points |
|---|---|---|
| `gexbot/gex_engine.py` | All GEX math: per-strike GEX, walls, flip, peak, multi-expiry structural aggregation. Pure transform + a standalone CLI/cron mode. | `build_gex_result()` (pure), `compute_gex()` (one-shot w/ I/O) |
| `gexbot/gex_stream_service.py` | Long-running daemon: one DXLink Greeks+Summary subscription for today's 0DTE SPX strikes; recompute every ~12s; write `gex_levels_live.json`. RTH-gated. | `main()`, `_run_session()` |
| `gexbot/pine_render.py` | **Pure** transform: a GEX frame dict → a Pine v6 indicator source string. No I/O, no network. | `render_pine(gex) -> str` |
| `gexbot/tv_publisher.py` | Daemon: read frame → `render_pine` → push via the TradingView CLI; re-push only when displayed levels move; backoff on failure. | `main()`, `_push()`, `_signature()` |
| `gexbot/tastytrade_client.py` | DXLink session/account from `TT_SECRET` / `TT_REFRESH`. | `get_session_and_account()` |
| `gexbot/spot_source.py` | SPX spot via yfinance (fallback when the DXLink cash-index tick is quiet). | `fetch_spot_via_yfinance()` |
| `gexbot/json_store.py` | Atomic JSON writes (temp + rename) so readers never see a half-written file. | `atomic_write_json()` |

`gex_engine.py` also has a `--json` / `--stream` CLI and writes a 15-min
`gex_levels.json` + a `logs/gex_history.jsonl` backtest log; that's the older
cold-open cadence. `tv_publisher` prefers the live file and falls back to the
15-min file when the stream daemon is down.

## The two contracts

Most of the design lives in two implicit contracts. Respect them when editing.

### 1. The frame JSON (`gex_levels_live.json`)

Produced by `build_gex_result()` (`gex_engine.py`), consumed by `pine_render` and
`tv_publisher`. The fields those consumers actually read:

```
spot            float            # SPX price
regime          str              # "POSITIVE_GAMMA" | "NEGATIVE_GAMMA"
product,expiry  str              # shown in the status box
timestamp       str (ISO ET)
put_wall        {strike,oi,volume}      # walls are dicts…
call_wall       {strike,oi,volume}
put_volume_wall {strike,volume}
call_volume_wall{strike,volume}
gamma_flip      float            # …flip/peak may be a bare float or {strike}
gamma_peak      {strike,gamma}
levels[]        [{strike, net_gex, call_oi, put_oi, ...}]   # per-strike profile
```

`pine_render._strike_of()` normalizes "dict-or-float". A canonical sample lives at
`tests/fixtures/gex_levels_live.sample.json` — use it for offline work.

### 2. Pine input stability (toggle preservation)

The indicator bakes **all** the data in as constants/arrays; what's *shown* is
controlled by `input.bool` toggles in TradingView's settings. The publisher
re-pushes the whole source on every data refresh. TradingView preserves a user's
input values across an "Update on chart" **only if the input structure is
unchanged** — so the toggle `input.*` titles and types in `render_pine` must stay
**stable across renders**; only the baked data constants may change. Changing a
toggle's title or reordering inputs will reset users' toggle choices on the next
refresh. There's a regression test for this (`test_toggles_present_and_stable`).

## External dependency: tradingview-mcp

`tv_publisher` does not talk to TradingView directly — it shells out to the
[tradingview-mcp](https://github.com/) Node CLI at `~/tradingview-mcp` (configurable
via `GEXBOT_TV_CLI` / `GEXBOT_TV_NODE`), which drives TradingView over CDP. We use
exactly two subcommands: `pine set --file <x.pine>` and `pine compile`. Two more are
gold for development:

- `node ~/tradingview-mcp/src/cli/index.js pine analyze -f x.pine` — **offline**
  static analysis, no TradingView needed.
- `node ~/tradingview-mcp/src/cli/index.js pine check -f x.pine` — **server-side**
  compile check, no chart needed.

Both let you validate generated Pine with the market closed and TradingView shut.

## Developer workflow

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# The renderer is pure — develop it entirely offline:
.venv/bin/python3 -m unittest tests.test_pine_render
.venv/bin/python3 -m gexbot.pine_render -f tests/fixtures/gex_levels_live.sample.json \
  | node ~/tradingview-mcp/src/cli/index.js pine check       # real compile, no market

.venv/bin/ruff check gexbot/ tests/                          # lint (config: ruff.toml)
```

The golden rule: **keep `pine_render.py` pure** (no I/O, no TradingView). All the
fiddly Pine generation is then unit-testable without a market or a chart. The
TradingView side (`tv_publisher`) is a thin, side-effecting shell around it.

### Common changes

- **Add a level / toggle:** add the baked constant + a stable-titled `input.bool`
  in `render_pine`, draw it under `if barstate.islast`, and add the displayed value
  to `_signature()` in `tv_publisher` so a change triggers a re-push. Update the
  toggle test.
- **Change the frame schema:** it's emitted by `build_gex_result` and read by
  `pine_render`/`tv_publisher`; update the fixture too.

## Runtime / ops

On the Mac, both daemons run as **launchd LaunchAgents** (user session, because
TradingView is a GUI app):

```
~/Library/LaunchAgents/com.gexbot.gex-stream.plist      → ~/Library/Logs/gexbot-gex-stream.log
~/Library/LaunchAgents/com.gexbot.tv-publisher.plist    → ~/Library/Logs/gexbot-tv-publisher.log
```

`RunAtLoad` + `KeepAlive` (relaunch at login, restart on crash). Credentials come
from `.env`; node + the CLI are passed as absolute paths because launchd's env is
minimal. Manage with `launchctl bootout|bootstrap gui/$(id -u) <plist>` and
`launchctl list | grep gexbot`. For a Linux box there are equivalent
`systemd/*.service` units.

## Gotchas & edge cases

- **CDP port must be re-enabled after any TradingView quit/reboot.** TradingView
  Desktop only exposes port 9222 when launched with `--remote-debugging-port=9222`.
  A normal reopen will *not* have it, and the publisher will report "unreachable"
  forever until you relaunch via the `TradingView Debug.app` shortcut (or
  `open -a "/Applications/TradingView.app" --args --remote-debugging-port=9222`).
- **First-time "Add to chart" is manual.** The publisher pushes + saves source,
  which updates the indicator *once it's on the chart*. The very first time, open
  the Pine editor and click **Add to chart**. After that the saved chart layout
  keeps it and every refresh updates in place.
- **`tv_publisher` failure handling.** `_push` returns `ok` / `unreachable` /
  `error`. `unreachable` (ECONNREFUSED / "No TradingView chart target" / timeout)
  → 5-min backoff with a rate-limited log, so a multi-day TV outage is cheap and
  self-recovers. `error` (compile error, editor-not-ready) → short exponential
  backoff. See `_classify()`.
- **SPX vs the chart symbol.** Levels are SPX *prices*. On an ES/SPY chart they're
  off by basis; the indicator's "Auto-anchor SPX → chart price" toggle shifts every
  level by `close - spot`.
- **RTH only.** Both daemons gate on regular trading hours (ET). 0DTE open interest
  is end-of-day, so off-hours the walls don't move; only gamma fields (net-GEX,
  flip, peak) move intraday.
- **Pin `tastytrade==12.2.0`.** 12.3+ has breaking session/streaming changes.

## History

Extracted from a larger trading system to run standalone. The
web→local-TradingView refactor landed 2026-06-11.
