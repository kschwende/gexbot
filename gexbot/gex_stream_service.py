#!/usr/bin/env python3
"""GEX Stream Service — persistent live 0DTE gamma for the TradingView overlay.

The 15-min cron (`agent/gex_engine.py`) cold-opens a DXLink connection,
subscribes ~1,000 option symbols, collects once, and writes
`agent/gex_levels.json`. That's the right cadence for the morning brief and
session plan, but it leaves the chart's gamma overlay frozen for 15 minutes.

This daemon holds ONE DXLink connection open with a persistent Greeks + Summary
subscription for today's 0DTE SPX strikes, keeps the latest event per symbol in
memory, and recomputes the GEX frame every ~12s (the measured DXLink Greeks
republish cadence is ~13s, so faster gains nothing). It writes a SEPARATE file,
`agent/gex_levels_live.json`, so the multi-expiry `gex_levels.json` and every
consumer that reads it are untouched. The TV publisher (``tv_publisher.py``)
prefers the live file when fresh and falls back to the 15-min file otherwise.

Scope: 0DTE only — that's what the chart overlay shows, and it keeps the held
subscription light (~200 symbols vs ~1,400 multi-expiry). Open interest is
end-of-day regardless, so only the gamma-derived fields (net-GEX, flip, peak)
actually move intraday; the walls/OI ride along unchanged.

Read-only market data: never places an order, never writes a producer store
other than its own live snapshot. RTH-gated. Reuses ``build_gex_result`` from
agent.gex_engine so the live frame is identical in shape to the cron's.

Run:
    cd /path/to/gexbot
    .venv/bin/python3 -u -m gexbot.gex_stream_service
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from gexbot.gex_engine import build_gex_result
from gexbot.json_store import atomic_write_json

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logger = logging.getLogger("gex_stream")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s gex_stream %(levelname)s %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

ET = ZoneInfo("America/New_York")
DATA_DIR = Path(os.environ.get("GEXBOT_DATA_DIR",
                               Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
LIVE_FILE = DATA_DIR / "gex_levels_live.json"

N_STRIKES = 100              # strikes around ATM (±50 → ±250pt window; spot won't escape intraday)
RECOMPUTE_SEC = 12           # tracks the ~13s DXLink Greeks republish cadence
RERESOLVE_SEC = 600          # re-pull the chain / re-center subscriptions every 10 min
SPOT_OUTLIER_FRAC = 0.03     # reject a Trade tick that jumps >3% from the running spot (bad SPX cash print)

RTH_OPEN = dtime(9, 30)
RTH_CLOSE = dtime(16, 0)


# ─── Live state ───────────────────────────────────────────────────────────
_greeks: dict = {}           # streamer_symbol -> Greeks event
_summaries: dict = {}        # streamer_symbol -> Summary event
_sym_map: dict = {}          # streamer_symbol -> (expiry_date, strike, 'C'|'P')
_spot: float | None = None
_spot_source: str | None = None
_today_exp: date | None = None
_state_lock = asyncio.Lock()


def _is_rth(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    t = now_et.timetz().replace(tzinfo=None)
    return RTH_OPEN <= t <= RTH_CLOSE


def _option_cp(s) -> str:
    """Call/put flag for an SPX Strike object, mirroring gex_engine's logic."""
    if hasattr(s, "option_type"):
        return "C" if s.option_type.value == "C" else "P"
    sym = getattr(s, "streamer_symbol", "") or ""
    try:
        if "P" in sym.split(str(date.today().year)[2:])[-1][:4]:
            return "P"
    except Exception:
        pass
    return "C"


async def _resolve_universe(session, spot: float) -> tuple[list[str], dict, date]:
    """Resolve today's 0DTE SPX strike universe around ``spot``.

    Returns (symbols, sym_map, today_expiry). The front expiry on/after today is
    treated as 0DTE (it IS today on a normal session)."""
    from tastytrade.instruments import get_option_chain

    chain = await get_option_chain(session, "SPX")
    today_exp = min(d for d in chain if d >= date.today())
    strikes_raw = chain[today_exp]
    all_sp = sorted({float(s.strike_price) for s in strikes_raw})
    if not all_sp:
        return [], {}, today_exp
    mid = min(range(len(all_sp)), key=lambda i: abs(all_sp[i] - spot))
    lo = max(0, mid - N_STRIKES // 2)
    hi = min(len(all_sp), mid + N_STRIKES // 2)
    window = set(all_sp[lo:hi])

    symbols: list[str] = []
    sym_map: dict = {}
    for s in strikes_raw:
        sp = float(s.strike_price)
        if sp not in window or not getattr(s, "streamer_symbol", None):
            continue
        symbols.append(s.streamer_symbol)
        sym_map[s.streamer_symbol] = (today_exp, sp, _option_cp(s))
    return symbols, sym_map, today_exp


async def _initial_spot(session) -> tuple[float | None, str | None]:
    """First spot — DXLink Trade tick, yfinance fallback (mirrors gex_engine)."""
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Trade
    try:
        async with DXLinkStreamer(session) as st:
            await st.subscribe(Trade, ["SPX"])
            t = await asyncio.wait_for(st.get_event(Trade), timeout=6)
            if t and t.price:
                return float(t.price), "dxlink"
    except Exception as exc:
        logger.warning("gex_stream: initial DXLink spot failed: %s", exc)
    try:
        from gexbot.spot_source import fetch_spot_via_yfinance
        s = fetch_spot_via_yfinance("SPX")
        if s:
            return float(s), "yfinance"
    except Exception as exc:
        logger.warning("gex_stream: yfinance spot fallback failed: %s", exc)
    return None, None


# ─── Listener coroutines ────────────────────────────────────────────────────
async def _consume_greeks(streamer, Greeks) -> None:
    async for g in streamer.listen(Greeks):
        if g.event_symbol in _sym_map:
            _greeks[g.event_symbol] = g


async def _consume_summaries(streamer, Summary) -> None:
    async for s in streamer.listen(Summary):
        if s.event_symbol in _sym_map:
            _summaries[s.event_symbol] = s


async def _consume_spot(streamer, Trade) -> None:
    """Update running spot from SPX Trade ticks, rejecting wild single-tick
    jumps (the cash index occasionally prints junk — see spot_source.py)."""
    global _spot
    async for t in streamer.listen(Trade):
        if t.event_symbol != "SPX" or not t.price:
            continue
        p = float(t.price)
        if _spot is not None and abs(p - _spot) / _spot > SPOT_OUTLIER_FRAC:
            logger.warning("gex_stream: rejected outlier spot %.2f (running %.2f)", p, _spot)
            continue
        _spot = p


async def _recompute_loop() -> None:
    """Every RECOMPUTE_SEC, build the GEX frame from the live event dicts and
    write the live snapshot file."""
    while True:
        await asyncio.sleep(RECOMPUTE_SEC)
        if _spot is None or _today_exp is None or not _sym_map:
            continue
        async with _state_lock:
            greeks = dict(_greeks)
            summaries = dict(_summaries)
            sym_map = dict(_sym_map)
            spot = _spot
            spot_src = _spot_source
            today_exp = _today_exp
        try:
            result = build_gex_result(
                greeks=greeks, summaries=summaries, sym_map=sym_map,
                target_expiries=[today_exp], live_spot=spot,
                spot_provenance=spot_src, product="SPX", expiry_date=today_exp,
            )
            if result.get("error"):
                logger.warning("gex_stream: recompute error: %s", result["error"])
                continue
            result["live"] = True
            result["recompute_sec"] = RECOMPUTE_SEC
            atomic_write_json(LIVE_FILE, result)
        except Exception as exc:
            logger.warning("gex_stream: recompute failed: %s", exc)


# ─── Main ───────────────────────────────────────────────────────────────────
async def _run_session() -> None:
    """One connected session: resolve universe, hold subscriptions, recompute.
    Returns (raises) on any stream error so the outer loop reconnects."""
    global _spot, _spot_source, _today_exp, _greeks, _summaries, _sym_map
    import os

    from tastytrade import DXLinkStreamer, Session
    from tastytrade.dxfeed import Greeks, Summary, Trade
    session = Session(provider_secret=os.environ.get("TT_SECRET"),
                      refresh_token=os.environ.get("TT_REFRESH"), is_test=False)

    spot, src = await _initial_spot(session)
    if spot is None:
        raise RuntimeError("no initial spot (DXLink + yfinance both failed)")
    _spot, _spot_source = spot, src

    symbols, sym_map, today_exp = await _resolve_universe(session, spot)
    if not symbols:
        raise RuntimeError("no 0DTE strikes resolved")
    async with _state_lock:
        _sym_map = sym_map
        _today_exp = today_exp
        _greeks = {}
        _summaries = {}
    logger.info("gex_stream: %d 0DTE symbols, expiry %s, spot %.2f (%s)",
                len(symbols), today_exp, spot, src)

    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Trade, ["SPX"])
        for i in range(0, len(symbols), 200):
            batch = symbols[i:i + 200]
            await streamer.subscribe(Greeks, batch)
            await streamer.subscribe(Summary, batch)
        logger.info("gex_stream: subscriptions live; recomputing every %ds", RECOMPUTE_SEC)

        async def reresolve_loop():
            global _sym_map, _today_exp
            while True:
                await asyncio.sleep(RERESOLVE_SEC)
                if _spot is None:
                    continue
                try:
                    syms, smap, texp = await _resolve_universe(session, _spot)
                except Exception as exc:
                    logger.warning("gex_stream: re-resolve failed: %s", exc)
                    continue
                new = [s for s in syms if s not in _sym_map]
                async with _state_lock:
                    _sym_map = smap
                    _today_exp = texp
                for i in range(0, len(new), 200):
                    b = new[i:i + 200]
                    await streamer.subscribe(Greeks, b)
                    await streamer.subscribe(Summary, b)
                if new:
                    logger.info("gex_stream: re-centered, +%d new symbols", len(new))

        await asyncio.gather(
            _consume_greeks(streamer, Greeks),
            _consume_summaries(streamer, Summary),
            _consume_spot(streamer, Trade),
            _recompute_loop(),
            reresolve_loop(),
        )


async def main() -> None:
    logger.info("gex_stream: starting")
    backoff = 2.0
    while True:
        now_et = datetime.now(ET)
        if not _is_rth(now_et):
            # Idle outside RTH; the 15-min cron owns gex_levels.json overnight.
            await asyncio.sleep(min(900, 60))
            continue
        try:
            await _run_session()
            backoff = 2.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("gex_stream: session error (%s); reconnect in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


if __name__ == "__main__":
    asyncio.run(main())
