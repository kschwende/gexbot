# gexbot

Standalone SPX **gamma chart** backend. Renders live GEX levels onto a
**local TradingView Desktop**
chart as a Pine indicator. **tastytrade DXLink only** (no Databento). Two
services:

- **`gex-stream`** — holds a persistent DXLink Greeks+Summary subscription for
  today's 0DTE SPX strikes and recomputes the GEX frame every ~12s (the measured
  Greeks republish cadence). Writes `data/gex_levels_live.json`.
- **`tv-publisher`** — reads that live frame, renders it into a self-contained
  Pine v6 indicator (all levels baked in + native `input.bool` toggles), and
  pushes it onto the running TradingView Desktop chart by shelling out to the
  [tradingview-mcp](https://github.com/) CLI over the CDP debug port (9222).
  Re-pushes only when the displayed levels move.

The indicator ships **all** the data — put/call walls, volume walls, gamma
flip, gamma peak, spot, and the per-strike net-GEX profile. What's *displayed*
is controlled by toggles in TradingView's indicator settings; flip them in the
UI, no round-trip. Those toggle values survive a data refresh (the input
structure is stable across renders).

> Replaces an older web path (an HTTP `chart-feed` service exposed over a
> tunnel). Nothing leaves the machine anymore.

**New to the code?** See [ARCHITECTURE.md](ARCHITECTURE.md) for the data flow,
component map, the two contracts, the dev/test loop, and the gotchas.

## Layout

```
gexbot/                  Python package (flat, self-contained)
  gex_stream_service.py  persistent 0DTE GEX streamer daemon
  gex_engine.py          GEX compute (build_gex_result + helpers)
  pine_render.py         GEX frame  → Pine v6 indicator source (pure, offline-testable)
  tv_publisher.py        push the rendered indicator onto local TradingView
  tastytrade_client.py   DXLink session
  spot_source.py         SPX spot (yfinance fallback)
  json_store.py          atomic JSON writes
systemd/                 unit files
tests/                   renderer unit tests + sample frame fixture
data/                    runtime output (gitignored)
```

## Prerequisites

- Python venv with `requirements.txt` (see below).
- **TradingView Desktop** running on the same machine with the CDP debug port
  open (9222), plus the **tradingview-mcp** CLI (`node`). The publisher calls
  `node <mcp>/src/cli/index.js pine set|compile`.

## Setup

```bash
# 1. venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Credentials
cp .env.example .env       # fill in TT_SECRET / TT_REFRESH
#   (optional) set GEXBOT_TV_CLI if tradingview-mcp isn't at ~/tradingview-mcp

# 3. Validate the renderer offline (no market / no TradingView needed)
.venv/bin/python3 -m unittest tests.test_pine_render
.venv/bin/python3 -m gexbot.pine_render -f tests/fixtures/gex_levels_live.sample.json \
  | node ~/tradingview-mcp/src/cli/index.js pine check   # server-side compile check
```

## Run (local, on the TradingView machine)

```bash
# Terminal 1 — the data source (market hours; writes data/gex_levels_live.json)
.venv/bin/python3 -m gexbot.gex_stream_service

# Terminal 2 — the publisher (renders + pushes onto the chart, refreshes ~30s)
.venv/bin/python3 -m gexbot.tv_publisher
```

### First-time chart setup

The publisher pushes source into the Pine editor and saves it, which updates
the indicator **once it's on the chart**. The very first time, add it manually:

1. Open the Pine editor in TradingView; the `GEXBot SPX Levels` source will be
   present after the publisher's first push.
2. Click **Add to chart**.

From then on every refresh updates it in place. Use it on an **SPX** chart (or
enable the indicator's *Auto-anchor* toggle to shift the SPX levels onto an
ES/SPY-proxy chart by `close − spot`).

## Indicator toggles

In the indicator's settings (group **GEX levels**): Put/Call walls, Volume
walls, Gamma flip, Gamma peak, Spot, Net-GEX profile, Labels, Status box.
Group **Alignment**: Auto-anchor SPX→chart, Manual offset. Group **Style**:
line length, profile width, per-level colors.

## Deploy (systemd, Linux box running TradingView Desktop)

```bash
# adjust paths/User in the unit files to match your install location
sudo cp systemd/gexbot-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gexbot-gex-stream gexbot-tv-publisher
```

## Config

| Var | Purpose |
|---|---|
| `TT_SECRET`, `TT_REFRESH` | tastytrade OAuth (brokerage market data) |
| `GEXBOT_DATA_DIR` | where the live snapshot is written/read (default `<repo>/data`) |
| `GEXBOT_TV_CLI` | path to the tradingview-mcp CLI (default `~/tradingview-mcp/src/cli/index.js`) |
| `GEXBOT_TV_NODE` | node binary (default `node`) |
| `GEXBOT_TV_REFRESH_SEC` | publisher refresh cadence in RTH (default `30`) |
| `GEXBOT_PINE_OUT` | where the generated `.pine` is written (default `<data>/gex_overlay.pine`) |

## Notes

- **Pin `tastytrade==12.2.0`** — 12.3+ has breaking session/streaming changes.
- 0DTE open interest is end-of-day; only gamma-derived fields (net-GEX, flip,
  peak) move intraday. The walls/OI ride along unchanged.
- `pine_render.py` is pure (no I/O, no TradingView): unit-test it offline and
  validate output with the MCP CLI's `pine analyze` (static) or `pine check`
  (server-side compile) — neither needs the market open.
- Extracted from a larger trading system to run standalone.
