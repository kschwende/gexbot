"""Multi-source spot-price fetching with sane fallbacks.

Canonical home for "give me the current spot of X" logic across producers.
Two callers exist today (``agent/gex_engine.py`` and
``agent/options_chain_live.py``) and both previously rolled their own
yfinance + DXLink pair; the 2026-05-06 11:15 ET phantom GEX-regime alert
showed that the structurally-identical fallback failure could hit either
one independently. Centralizing here ensures the next module that needs
SPX/ES spot inherits both the lesson and the fallback.

Symbol mapping (product code → yfinance ticker) handles the cases where
a tastytrade-style symbol (e.g. ``SPX``) is not the same as the yfinance
ticker (``^GSPC``). Pass an already-yfinance ticker through and it just
works (``YFINANCE_SYMBOLS.get`` falls through to the input).

Both functions are fail-soft: returning ``None`` means "this source
didn't yield, try the next one" — never raises.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


# Product / tastytrade-symbol → yfinance ticker.
# Anything not in this map passes through unchanged, so a yfinance ticker
# like "^GSPC" or "SPY" works directly without a map entry.
YFINANCE_SYMBOLS: dict[str, str] = {
    # Indices
    "SPX": "^GSPC",
    "VIX": "^VIX",
    "DXY": "DX-Y.NYB",
    "TNX": "^TNX",
    # Front-month futures (continuous contract)
    "ES": "ES=F",
    "NQ": "NQ=F",
    "RTY": "RTY=F",
    "YM": "YM=F",
    "CL": "CL=F",
    "BRENT": "BZ=F",
    "GC": "GC=F",
    "BTC": "BTC-USD",
}


def fetch_spot_via_yfinance(symbol: str) -> float | None:
    """Spot lookup via yfinance. Typical latency 100-300ms, no auth.

    ``symbol`` may be a product code (``SPX``, ``ES``, ``VIX``) which gets
    mapped via :data:`YFINANCE_SYMBOLS`, or an already-yfinance ticker
    (``^GSPC``, ``SPY``) which passes through.

    Returns the last available price as a float, or ``None`` on any
    failure (yfinance not installed, network error, empty history,
    invalid symbol). Callers should treat ``None`` as "fall through to
    next source," not as an error worth raising.
    """
    yf_symbol = YFINANCE_SYMBOLS.get(symbol, symbol)
    try:
        import yfinance as yf
        tkr = yf.Ticker(yf_symbol)
        fi = getattr(tkr, "fast_info", None)
        last = None
        if fi is not None:
            try:
                last = (
                    fi.get("last_price") if hasattr(fi, "get")
                    else getattr(fi, "last_price", None)
                )
            except Exception:
                last = None
        if last:
            return float(last)
        # fast_info miss — fall back to a 1-min bar pull
        hist = tkr.history(period="1d", interval="1m")
        if hist is not None and not hist.empty:
            return float(hist["Close"].dropna().iloc[-1])
    except Exception as exc:
        logger.debug("yfinance spot failed for %s (mapped from %s): %s",
                     yf_symbol, symbol, exc)
    return None


async def fetch_spot_via_dxlink(
    streamer, symbol: str, timeout: float = 5.0,
) -> float | None:
    """Spot from a tastytrade DXLink Trade subscription.

    ``streamer`` must be an open ``DXLinkStreamer`` — this function does
    not manage its lifecycle. ``symbol`` is a tastytrade streamer symbol
    (``/ES:XCME`` for ES futures, ``SPX`` for the cash index, etc).

    Returns the last-trade price or ``None`` on subscription failure /
    timeout. Index-quoted symbols like ``SPX`` can go quiet for stretches
    (the cash index is calculated, not trade-driven), so always pair a
    DXLink call with a yfinance fallback when fetching SPX spot.
    """
    try:
        from tastytrade.dxfeed import Trade
    except Exception as exc:  # pragma: no cover — env-dependent
        logger.debug("tastytrade.dxfeed unavailable: %s", exc)
        return None

    try:
        await streamer.subscribe(Trade, [symbol])
        t = await asyncio.wait_for(streamer.get_event(Trade), timeout=timeout)
        if t and getattr(t, "price", None):
            return float(t.price)
    except Exception as exc:
        logger.debug("DXLink spot failed for %s: %s", symbol, exc)
    return None
