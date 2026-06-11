#!/usr/bin/env python3
"""
GEX Engine — Gamma Exposure & Dealer Flow from tastytrade DXLink
==================================================================
Computes from live Greeks + OI + Volume across the front 5-7 SPX expiries:
  - GEX per strike aggregated across expiries (gamma × OI × 100 × spot)
  - Put/call walls — both 0DTE (intraday) and structural (multi-expiry)
  - Gamma flip — both 0DTE and structural
  - Net GEX regime (positive = dealers dampen, negative = dealers amplify)
  - Charm/vanna values for PM session planning
  - ATM straddle and expected range

The multi-expiry aggregation surfaces durable weekly/monthly positioning
that the 0DTE-only view misses. Near-dated gamma naturally dominates —
no artificial time-weighting. See 2026-04-15 refactor for the fix history.

Usage:
  python3 gex_engine.py              # Snapshot (SPX, front 7 days)
  python3 gex_engine.py --json       # JSON for MCP
  python3 gex_engine.py --stream     # Update every 5 min
  python3 gex_engine.py --es         # ES futures options path
"""

import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from gexbot.json_store import atomic_write_json

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

ET = ZoneInfo("America/New_York")
DATA_DIR = Path(os.environ.get("GEXBOT_DATA_DIR",
                               Path(__file__).resolve().parent.parent / "data"))
GEX_FILE = DATA_DIR / "gex_levels.json"

DEFAULT_DAYS_FORWARD = 7
DEFAULT_MAX_EXPIRIES = 7


# ---------------------------------------------------------------------------
# Expiry selection
# ---------------------------------------------------------------------------


def _select_spx_expiries(chain, start_date, days_forward, max_expiries):
    """Pick the front N SPX expiries within the calendar window.

    ``chain`` is the mapping returned by ``get_option_chain(session, "SPX")``
    — a dict of ``{date: [Strike, ...]}``. We want expiries in
    ``[start_date, start_date + days_forward]`` up to ``max_expiries``.
    """
    end_date = start_date + timedelta(days=days_forward)
    candidates = sorted(d for d in chain if start_date <= d <= end_date)
    return candidates[:max_expiries]


def _select_es_expiries(chain, start_date, days_forward, max_expiries):
    """Pick the front N ES futures-options expiries within the window.

    ``chain`` is a ``NestedFutureOptionChain`` (or its first element when
    the API returns a list). We walk ``option_chains[*].expirations`` and
    filter by date.
    """
    end_date = start_date + timedelta(days=days_forward)
    found: list[tuple] = []  # (exp_date, expiration_obj)
    seen_dates: set = set()
    for oc in chain.option_chains:
        for exp in oc.expirations:
            exp_d = exp.expiration_date
            if start_date <= exp_d <= end_date and exp_d not in seen_dates:
                found.append((exp_d, exp))
                seen_dates.add(exp_d)
    found.sort(key=lambda t: t[0])
    return found[:max_expiries]


# ---------------------------------------------------------------------------
# Per-strike GEX computation
# ---------------------------------------------------------------------------


def _compute_gex_for_strike(d, spot):
    """Apply the GEX formula to a merged {call_gamma, put_gamma, ...} dict.

    Formula (per-$1 spot move, dollar-denominated):
        call_gex =  call_gamma × call_oi × 100 × spot
        put_gex  = -put_gamma  × put_oi  × 100 × spot
        net_gex  = call_gex + put_gex

    Calls contribute POSITIVE GEX (dealers long gamma if hedged short calls).
    Puts contribute NEGATIVE GEX (dealers short put gamma). Convention:
    positive net_gex = dealers long gamma overall = moves dampened.
    """
    cg = d.get("call_gamma", 0) or 0
    pg = d.get("put_gamma", 0) or 0
    coi = d.get("call_oi", 0) or 0
    poi = d.get("put_oi", 0) or 0
    call_gex = cg * coi * 100 * spot
    put_gex = -pg * poi * 100 * spot
    return call_gex, put_gex, call_gex + put_gex


def _build_level_rows(strike_data, spot):
    """Turn a ``{strike: {call_gamma, ...}}`` dict into a list of level rows
    suitable for JSON output and wall/flip analysis.
    """
    levels = []
    for sp in sorted(strike_data.keys()):
        d = strike_data[sp]
        cg = d.get("call_gamma", 0) or 0
        pg = d.get("put_gamma", 0) or 0
        coi = d.get("call_oi", 0) or 0
        poi = d.get("put_oi", 0) or 0
        cvol = d.get("call_volume", 0) or 0
        pvol = d.get("put_volume", 0) or 0

        call_gex, put_gex, net_gex = _compute_gex_for_strike(d, spot)

        charm_call = abs(d.get("call_theta", 0) or 0) / max(abs(d.get("call_delta", 0.01) or 0.01), 0.01)
        charm_put = abs(d.get("put_theta", 0) or 0) / max(abs(d.get("put_delta", 0.01) or 0.01), 0.01)

        levels.append({
            "strike": sp,
            "call_gamma": round(cg, 6), "put_gamma": round(pg, 6),
            "call_oi": coi, "put_oi": poi,
            "call_volume": cvol, "put_volume": pvol,
            "call_gex": round(call_gex, 0), "put_gex": round(put_gex, 0),
            "net_gex": round(net_gex, 0),
            "call_delta": round(d.get("call_delta", 0) or 0, 4),
            "put_delta": round(d.get("put_delta", 0) or 0, 4),
            "call_iv": round(d.get("call_iv", 0) or 0, 1),
            "put_iv": round(d.get("put_iv", 0) or 0, 1),
            "call_price": round(d.get("call_price", 0) or 0, 2),
            "put_price": round(d.get("put_price", 0) or 0, 2),
            "charm_call": round(charm_call, 2),
            "charm_put": round(charm_put, 2),
            "total_volume": cvol + pvol,
            "total_oi": coi + poi,
        })
    return levels


def _find_walls(levels, spot):
    """Find call/put/volume walls relative to spot. Returns a dict with
    None-safe fields. Calls above spot, puts below."""
    puts_below = [l for l in levels if l["strike"] < spot and l["put_oi"] > 0]
    calls_above = [l for l in levels if l["strike"] > spot and l["call_oi"] > 0]

    put_wall = max(puts_below, key=lambda x: x["put_oi"] * x["put_gamma"]) if puts_below else None
    call_wall = max(calls_above, key=lambda x: x["call_oi"] * x["call_gamma"]) if calls_above else None
    put_volume_wall = max(puts_below, key=lambda x: x["put_volume"]) if puts_below else None
    call_volume_wall = max(calls_above, key=lambda x: x["call_volume"]) if calls_above else None

    return {
        "put_wall": {"strike": put_wall["strike"], "oi": put_wall["put_oi"],
                     "volume": put_wall["put_volume"]} if put_wall else None,
        "call_wall": {"strike": call_wall["strike"], "oi": call_wall["call_oi"],
                      "volume": call_wall["call_volume"]} if call_wall else None,
        "put_volume_wall": {"strike": put_volume_wall["strike"],
                            "volume": put_volume_wall["put_volume"]} if put_volume_wall else None,
        "call_volume_wall": {"strike": call_volume_wall["strike"],
                             "volume": call_volume_wall["call_volume"]} if call_volume_wall else None,
    }


def _find_gamma_flip(levels, spot):
    """Gamma flip = the strike nearest to spot where net_gex crosses from
    positive to negative as strikes increase. Returns a float or None.
    """
    sorted_levels = sorted(levels, key=lambda x: x["strike"])
    gamma_flip = None
    best_dist = float("inf")
    for i in range(1, len(sorted_levels)):
        if sorted_levels[i - 1]["net_gex"] > 0 and sorted_levels[i]["net_gex"] <= 0:
            flip_strike = sorted_levels[i]["strike"]
            dist = abs(flip_strike - spot)
            if dist < best_dist:
                best_dist = dist
                gamma_flip = flip_strike
    return gamma_flip


def _summarize_expiry(strike_data, spot):
    """Full per-expiry analysis: levels + walls + flip + total + regime.
    Does NOT write files — pure function for testing + aggregation reuse.
    """
    levels = _build_level_rows(strike_data, spot)
    total_gex = sum(l["net_gex"] for l in levels)
    walls = _find_walls(levels, spot)
    gamma_flip = _find_gamma_flip(levels, spot)
    gamma_peak = max(levels, key=lambda x: x["call_gamma"] + x["put_gamma"]) if levels else None

    return {
        "levels": levels,
        "total_gex": round(total_gex, 0),
        "regime": "POSITIVE_GAMMA" if total_gex > 0 else "NEGATIVE_GAMMA",
        "gamma_flip": gamma_flip,
        "gamma_peak": {"strike": gamma_peak["strike"],
                       "gamma": round(gamma_peak["call_gamma"] + gamma_peak["put_gamma"], 6)}
                      if gamma_peak else None,
        "put_wall": walls["put_wall"],
        "call_wall": walls["call_wall"],
        "put_volume_wall": walls["put_volume_wall"],
        "call_volume_wall": walls["call_volume_wall"],
    }


# ---------------------------------------------------------------------------
# Multi-expiry structural aggregation
# ---------------------------------------------------------------------------


def _aggregate_structural(by_expiry_strike_data, spot):
    """Sum strike_data across expiries into a combined per-strike dict.

    For each strike, sum call_oi/put_oi/call_volume/put_volume across
    expiries. Sum gamma * OI contributions (so the per-strike structural
    gamma is OI-weighted across expiries — larger, more durable positioning
    dominates). Greeks like delta/IV/price/theta are averaged weighted by
    OI (purely informational).

    Near-dated expiries naturally dominate via their larger gamma values
    (no artificial time weighting needed).

    Returns a dict: ``{strike: merged_strike_data_dict}`` suitable to feed
    back into ``_build_level_rows`` / ``_summarize_expiry``.
    """
    merged = {}

    def _wmean(existing, new_val, existing_w, new_w):
        """Running OI-weighted mean update."""
        total_w = existing_w + new_w
        if total_w == 0:
            return existing
        return (existing * existing_w + new_val * new_w) / total_w

    for _exp_d, strike_data in by_expiry_strike_data.items():
        for sp, d in strike_data.items():
            if sp not in merged:
                merged[sp] = {
                    "call_gamma_oi_sum": 0.0,  # sum(call_gamma * call_oi) for OI-weighted gamma
                    "put_gamma_oi_sum": 0.0,
                    "call_oi_total": 0,
                    "put_oi_total": 0,
                    "call_volume_total": 0,
                    "put_volume_total": 0,
                    "call_delta": 0.0,  # OI-weighted avg
                    "put_delta": 0.0,
                    "call_iv": 0.0,
                    "put_iv": 0.0,
                    "call_price": 0.0,
                    "put_price": 0.0,
                    "call_theta": 0.0,
                    "put_theta": 0.0,
                    "n_expiries": 0,
                }
            m = merged[sp]

            cg = d.get("call_gamma", 0) or 0
            pg = d.get("put_gamma", 0) or 0
            coi = d.get("call_oi", 0) or 0
            poi = d.get("put_oi", 0) or 0

            # OI-weighted gamma: we accumulate (gamma * OI) and (OI), then
            # the final call_gamma = sum(gamma_i * oi_i) / sum(oi_i).
            m["call_gamma_oi_sum"] += cg * coi
            m["put_gamma_oi_sum"] += pg * poi

            # Running OI-weighted averages for informational greeks
            new_coi = m["call_oi_total"] + coi
            new_poi = m["put_oi_total"] + poi
            if new_coi > 0:
                m["call_delta"] = _wmean(m["call_delta"], d.get("call_delta", 0) or 0,
                                          m["call_oi_total"], coi)
                m["call_iv"] = _wmean(m["call_iv"], d.get("call_iv", 0) or 0,
                                       m["call_oi_total"], coi)
                m["call_price"] = _wmean(m["call_price"], d.get("call_price", 0) or 0,
                                          m["call_oi_total"], coi)
                m["call_theta"] = _wmean(m["call_theta"], d.get("call_theta", 0) or 0,
                                          m["call_oi_total"], coi)
            if new_poi > 0:
                m["put_delta"] = _wmean(m["put_delta"], d.get("put_delta", 0) or 0,
                                         m["put_oi_total"], poi)
                m["put_iv"] = _wmean(m["put_iv"], d.get("put_iv", 0) or 0,
                                      m["put_oi_total"], poi)
                m["put_price"] = _wmean(m["put_price"], d.get("put_price", 0) or 0,
                                         m["put_oi_total"], poi)
                m["put_theta"] = _wmean(m["put_theta"], d.get("put_theta", 0) or 0,
                                         m["put_oi_total"], poi)

            m["call_oi_total"] = new_coi
            m["put_oi_total"] = new_poi
            m["call_volume_total"] += d.get("call_volume", 0) or 0
            m["put_volume_total"] += d.get("put_volume", 0) or 0
            m["n_expiries"] += 1

    # Flatten into {strike: shape-compatible dict for _build_level_rows}
    out = {}
    for sp, m in merged.items():
        coi = m["call_oi_total"]
        poi = m["put_oi_total"]
        call_gamma = (m["call_gamma_oi_sum"] / coi) if coi > 0 else 0.0
        put_gamma = (m["put_gamma_oi_sum"] / poi) if poi > 0 else 0.0
        out[sp] = {
            "call_gamma": call_gamma,
            "put_gamma": put_gamma,
            "call_oi": coi,
            "put_oi": poi,
            "call_volume": m["call_volume_total"],
            "put_volume": m["put_volume_total"],
            "call_delta": m["call_delta"],
            "put_delta": m["put_delta"],
            "call_iv": m["call_iv"],
            "put_iv": m["put_iv"],
            "call_price": m["call_price"],
            "put_price": m["put_price"],
            "call_theta": m["call_theta"],
            "put_theta": m["put_theta"],
            "n_expiries": m["n_expiries"],
        }
    return out


# ---------------------------------------------------------------------------
# Main compute path — multi-expiry aware
# ---------------------------------------------------------------------------


async def compute_gex(n_strikes=100, expiry_date=None, product="SPX",
                       days_forward=DEFAULT_DAYS_FORWARD,
                       max_expiries=DEFAULT_MAX_EXPIRIES):
    """Full GEX computation from live tastytrade DXLink data.

    Args:
        n_strikes: number of strikes to analyze around ATM per expiry
        expiry_date: legacy single-expiry anchor (default: today). Used as
            the anchor for the 0DTE primary view AND as the start of the
            multi-expiry window.
        product: "SPX" for index options, "ES" for futures options
        days_forward: calendar days past ``expiry_date`` to include
        max_expiries: hard cap on the number of expiries aggregated
    """
    from tastytrade import Session
    from tastytrade.dxfeed import Greeks, Summary, Trade
    from tastytrade.instruments import NestedFutureOptionChain, get_option_chain
    from tastytrade.streamer import DXLinkStreamer

    secret = os.environ.get("TT_SECRET")
    refresh = os.environ.get("TT_REFRESH")
    session = Session(provider_secret=secret, refresh_token=refresh, is_test=False)

    # 1. Live spot — DXLink first, yfinance fallback, then abort.
    #
    # The DXLink Trade feed for `SPX` (the cash index) sometimes fails to
    # deliver an initial tick within the 5s budget — SPX is index-quoted,
    # not trade-quoted, so the symbol can go quiet for stretches. The
    # 2026-05-06 11:15 ET incident traced to this: live_spot=None →
    # strike pre-selection fell back to median-of-chain (~6960) → engine
    # produced a fully-coherent-but-wrong frame (atm_iv 75.4, walls
    # hugging the bogus spot, regime flipped) that fired phantom
    # POSITIVE→NEGATIVE then NEGATIVE→POSITIVE alerts to Karl.
    # agent/options_chain_live.py learned this lesson previously; this
    # mirrors that fix here.
    live_spot = None
    spot_provenance = None  # 'dxlink' | 'yfinance' — distinguishes the path
    spot_symbol = "/ES:XCME" if product == "ES" else "SPX"
    try:
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Trade, [spot_symbol])
            t = await asyncio.wait_for(streamer.get_event(Trade), timeout=5)
            if t and t.price:
                live_spot = float(t.price)
                spot_provenance = "dxlink"
    except Exception as e:
        print(f"  Warning: Could not get live spot for {spot_symbol}: {e}", flush=True)

    if live_spot is None:
        from gexbot.spot_source import fetch_spot_via_yfinance
        live_spot = fetch_spot_via_yfinance(product)
        if live_spot:
            spot_provenance = "yfinance"
            print(f"  Spot fallback: yfinance for {product} = {live_spot}", flush=True)

    if live_spot is None:
        return {
            "error": (
                f"No spot price available for {product}: DXLink {spot_symbol} "
                "and yfinance both failed. Aborting to avoid emitting a "
                "median-strike-fallback frame (would corrupt regime/walls/IV)."
            )
        }

    if expiry_date is None:
        expiry_date = date.today()

    # 2. Select the front N expiries
    target_expiries: list[date] = []
    # per-expiry strike objects keyed by date:
    strikes_by_expiry: dict[date, list] = {}

    if product == "ES":
        chain = await NestedFutureOptionChain.get(session, "ES")
        if isinstance(chain, list):
            chain = chain[0] if chain else None
        if chain is None:
            return {"error": "No ES NestedFutureOptionChain available"}

        exp_tuples = _select_es_expiries(chain, expiry_date, days_forward, max_expiries)
        if not exp_tuples:
            # Widen the window up to 14 days if the near window is empty
            exp_tuples = _select_es_expiries(chain, expiry_date, 14, max_expiries)
        for exp_d, exp_obj in exp_tuples:
            target_expiries.append(exp_d)
            strikes_by_expiry[exp_d] = exp_obj.strikes
    else:
        chain = await get_option_chain(session, "SPX")
        target_expiries = _select_spx_expiries(chain, expiry_date, days_forward, max_expiries)
        if not target_expiries:
            target_expiries = _select_spx_expiries(chain, expiry_date, 14, max_expiries)
        for d_ in target_expiries:
            strikes_by_expiry[d_] = chain.get(d_, [])

    if not target_expiries:
        return {"error": f"No {product} expiries found in the {days_forward}d window from {expiry_date}"}

    print(f"  Multi-expiry GEX: {product} {len(target_expiries)} expiries "
          f"({target_expiries[0]} → {target_expiries[-1]})", flush=True)

    # 3. Assemble symbol list across all expiries, centered on spot per-expiry
    symbols: list[str] = []
    sym_map: dict[str, tuple] = {}  # symbol -> (exp_date, strike, 'C'|'P')

    for exp_d in target_expiries:
        strikes_raw = strikes_by_expiry[exp_d]
        if not strikes_raw:
            continue

        all_sp = sorted(set(float(s.strike_price) for s in strikes_raw))
        if live_spot:
            mid_idx = min(range(len(all_sp)), key=lambda i: abs(all_sp[i] - live_spot))
        else:
            mid_idx = len(all_sp) // 2
        lo = max(0, mid_idx - n_strikes // 2)
        hi = min(len(all_sp), mid_idx + n_strikes // 2)
        target = set(all_sp[lo:hi])

        for s in strikes_raw:
            sp = float(s.strike_price)
            if sp not in target:
                continue
            if product == "ES":
                if getattr(s, "put_streamer_symbol", None):
                    symbols.append(s.put_streamer_symbol)
                    sym_map[s.put_streamer_symbol] = (exp_d, sp, "P")
                if getattr(s, "call_streamer_symbol", None):
                    symbols.append(s.call_streamer_symbol)
                    sym_map[s.call_streamer_symbol] = (exp_d, sp, "C")
            else:
                cp = "C"
                if hasattr(s, "option_type"):
                    cp = "C" if s.option_type.value == "C" else "P"
                else:
                    # Fallback parse: "P" flag in the segment after the year
                    try:
                        if "P" in s.streamer_symbol.split(str(exp_d.year)[2:])[-1][:4]:
                            cp = "P"
                    except Exception:
                        pass
                symbols.append(s.streamer_symbol)
                sym_map[s.streamer_symbol] = (exp_d, sp, cp)

    if not symbols:
        return {"error": f"No strikes near spot found for any of {target_expiries}"}

    # 4. Subscribe to all symbols in batches; collect Greeks + Summary
    greeks: dict = {}
    summaries: dict = {}
    # Deadline scales with symbol count. Baseline 20s per 200 symbols, capped.
    deadline_sec = min(60, max(20, len(symbols) // 40 + 15))

    async with DXLinkStreamer(session) as streamer:
        for i in range(0, len(symbols), 200):
            batch = symbols[i:i + 200]
            await streamer.subscribe(Greeks, batch)
            await streamer.subscribe(Summary, batch)

        deadline = asyncio.get_event_loop().time() + deadline_sec
        target_count = max(1, int(len(symbols) * 0.7))

        async def collect_greeks():
            async for g in streamer.listen(Greeks):
                if g.event_symbol in sym_map:
                    greeks[g.event_symbol] = g
                if len(greeks) >= target_count:
                    break
                if asyncio.get_event_loop().time() > deadline:
                    break

        async def collect_summaries():
            async for s in streamer.listen(Summary):
                if s.event_symbol in sym_map:
                    summaries[s.event_symbol] = s
                if len(summaries) >= target_count:
                    break
                if asyncio.get_event_loop().time() > deadline:
                    break

        await asyncio.gather(collect_greeks(), collect_summaries())

    print(f"  Greeks: {len(greeks)} | Summaries: {len(summaries)} "
          f"(target {target_count} of {len(symbols)}, deadline {deadline_sec}s)", flush=True)

    # 5–11. Pure transform → result dict. Shared with the persistent
    # gex-stream daemon (bot/gex_stream_service.py) so both emit identical
    # frames; see build_gex_result below.
    result = build_gex_result(
        greeks=greeks, summaries=summaries, sym_map=sym_map,
        target_expiries=target_expiries, live_spot=live_spot,
        spot_provenance=spot_provenance, product=product, expiry_date=expiry_date,
    )
    if result.get("error"):
        return result

    atomic_write_json(GEX_FILE, result)
    _append_gex_history(result, product)
    return result


def build_gex_result(*, greeks, summaries, sym_map, target_expiries,
                     live_spot, spot_provenance, product, expiry_date):
    """Pure transform from collected Greeks/Summary events to the
    gex_levels.json result dict — no I/O, no awaits, no file write. Shared by
    the one-shot ``compute_gex`` and the persistent gex-stream daemon so both
    emit identical frames. ``greeks``/``summaries`` map streamer_symbol → event;
    ``sym_map`` maps streamer_symbol → (expiry_date, strike, 'C'|'P'). Returns
    the result dict, or {"error": ...} when there's no usable spot/strike data.
    """
    # 5. Merge into per-expiry strike data
    by_expiry_strike_data: dict[date, dict[float, dict]] = {d: {} for d in target_expiries}
    for sym in set(list(greeks.keys()) + list(summaries.keys())):
        if sym not in sym_map:
            continue
        exp_d, sp, cp = sym_map[sym]
        if sp not in by_expiry_strike_data[exp_d]:
            by_expiry_strike_data[exp_d][sp] = {}
        d = by_expiry_strike_data[exp_d][sp]

        g = greeks.get(sym)
        s = summaries.get(sym)
        prefix = "call" if cp == "C" else "put"

        if g:
            d[f"{prefix}_gamma"] = float(g.gamma) if g.gamma else 0
            d[f"{prefix}_delta"] = float(g.delta) if g.delta else 0
            d[f"{prefix}_theta"] = float(g.theta) if g.theta else 0
            d[f"{prefix}_vega"] = float(g.vega) if g.vega else 0
            d[f"{prefix}_iv"] = float(g.volatility) * 100 if g.volatility else 0
            d[f"{prefix}_price"] = float(g.price) if g.price else 0
        if s:
            d[f"{prefix}_oi"] = int(s.open_interest) if s.open_interest else 0
            d[f"{prefix}_volume"] = int(s.prev_day_volume) if s.prev_day_volume else 0

    # 6. Resolve spot — live if available, else delta-estimate from nearest expiry
    spot = live_spot
    if not spot:
        # Try the nearest expiry's strikes for a delta-50 strike
        for exp_d in target_expiries:
            for sp, d in by_expiry_strike_data[exp_d].items():
                cd = d.get("call_delta", 0) or 0
                if 0.45 < abs(cd) < 0.55:
                    spot = sp
                    break
            if spot:
                break
    if not spot:
        # Last-resort: median strike from the first populated expiry
        for exp_d in target_expiries:
            if by_expiry_strike_data[exp_d]:
                sks = sorted(by_expiry_strike_data[exp_d].keys())
                spot = sks[len(sks) // 2]
                break
    if not spot:
        return {"error": "No spot price and no strike data to estimate from"}

    # 7. Per-expiry analysis
    expiry_summaries: dict[date, dict] = {}
    for exp_d in target_expiries:
        sd = by_expiry_strike_data[exp_d]
        if not sd:
            continue
        expiry_summaries[exp_d] = _summarize_expiry(sd, spot)

    if not expiry_summaries:
        return {"error": "No expiries yielded any strike data"}

    # 8. Structural aggregation
    structural_strike_data = _aggregate_structural(by_expiry_strike_data, spot)
    structural = _summarize_expiry(structural_strike_data, spot)

    # 9. Select the 0DTE primary view
    # Preferred: the requested expiry_date. Fallback: the nearest available.
    primary_exp = expiry_date if expiry_date in expiry_summaries else target_expiries[0]
    primary = expiry_summaries[primary_exp]
    primary_levels = primary["levels"]

    # 10. ATM straddle + charm from the 0DTE view
    atm = min(primary_levels, key=lambda x: abs(x["strike"] - spot))
    atm_straddle = atm["call_price"] + atm["put_price"]
    expected_range = atm_straddle / spot * 100 if spot else 0
    high_charm_puts = sorted([l for l in primary_levels if l["strike"] < spot],
                              key=lambda x: x["charm_put"], reverse=True)[:5]
    high_charm_calls = sorted([l for l in primary_levels if l["strike"] > spot],
                               key=lambda x: x["charm_call"], reverse=True)[:5]

    # 11. Assemble result — keep primary fields backward-compatible, add new
    strikes_analyzed = len(by_expiry_strike_data[primary_exp])

    # Compact per-expiry breakdown for the output (no full strike lists)
    levels_by_expiry_out = {}
    for exp_d, summary in expiry_summaries.items():
        levels_by_expiry_out[str(exp_d)] = {
            "n_strikes": len(summary["levels"]),
            "total_gex": summary["total_gex"],
            "regime": summary["regime"],
            "gamma_flip": summary["gamma_flip"],
            "call_wall": summary["call_wall"],
            "put_wall": summary["put_wall"],
        }

    result = {
        # PRIMARY (backward-compatible 0DTE view)
        "timestamp": datetime.now(ET).isoformat(),
        "product": product,
        "expiry": str(primary_exp),
        "spot": round(spot, 2),
        # Provenance values: "dxlink" (live tick), "yfinance" (fallback,
        # also used for SPX cash close overnight when index is quiet),
        # "estimated" (no live source — delta-50 / median-strike fallback,
        # which only triggers if the abort guard is bypassed somehow).
        "spot_source": spot_provenance if live_spot else "estimated",
        "strikes_analyzed": strikes_analyzed,
        "regime": primary["regime"],
        "total_gex": primary["total_gex"],
        "gamma_peak": primary["gamma_peak"],
        "put_wall": primary["put_wall"],
        "call_wall": primary["call_wall"],
        "put_volume_wall": primary["put_volume_wall"],
        "call_volume_wall": primary["call_volume_wall"],
        "gamma_flip": primary["gamma_flip"],
        "atm_straddle": round(atm_straddle, 2),
        "atm_iv": round(atm["call_iv"], 1),
        "expected_range_pct": round(expected_range, 2),
        "charm_hotspots": {
            "puts": [{"strike": l["strike"], "charm": l["charm_put"]} for l in high_charm_puts],
            "calls": [{"strike": l["strike"], "charm": l["charm_call"]} for l in high_charm_calls],
        },
        "levels": primary_levels,

        # NEW: multi-expiry aggregation
        "expiries_analyzed": [str(d) for d in target_expiries],
        "n_expiries": len(expiry_summaries),
        "gamma_flip_0dte": primary["gamma_flip"],
        "gamma_flip_structural": structural["gamma_flip"],
        "call_wall_0dte": primary["call_wall"],
        "put_wall_0dte": primary["put_wall"],
        "call_wall_structural": structural["call_wall"],
        "put_wall_structural": structural["put_wall"],
        "gamma_peak_structural": structural["gamma_peak"],
        "structural_total_gex": structural["total_gex"],
        "structural_regime": structural["regime"],
        "levels_structural": structural["levels"],
        "levels_by_expiry": levels_by_expiry_out,
    }

    return result


def _append_gex_history(result, product):
    """Append a compact daily snapshot to the JSONL history log (backtesting).
    Best-effort — never raises into the caller."""
    try:
        log_dir = Path(__file__).parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        history_file = log_dir / "gex_history.jsonl"
        snapshot = {
            "date": str(date.today()),
            "timestamp": result["timestamp"],
            "product": product,
            "regime": result["regime"],
            "total_gex": result["total_gex"],
            "structural_regime": result["structural_regime"],
            "structural_total_gex": result["structural_total_gex"],
            "put_wall": result["put_wall"]["strike"] if result.get("put_wall") else None,
            "call_wall": result["call_wall"]["strike"] if result.get("call_wall") else None,
            "put_wall_structural": result["put_wall_structural"]["strike"] if result.get("put_wall_structural") else None,
            "call_wall_structural": result["call_wall_structural"]["strike"] if result.get("call_wall_structural") else None,
            "gamma_flip_0dte": result.get("gamma_flip_0dte"),
            "gamma_flip_structural": result.get("gamma_flip_structural"),
            "spot": result["spot"],
            "spot_source": result.get("spot_source"),
            "n_expiries": result["n_expiries"],
            "atm_straddle": result.get("atm_straddle"),
            "atm_iv": result.get("atm_iv"),
            "expected_range_pct": result.get("expected_range_pct"),
        }
        with open(history_file, "a") as f:
            f.write(json.dumps(snapshot, default=str) + "\n")
    except Exception as e:
        print(f"  Warning: Failed to append GEX history log: {e}", flush=True)


def print_summary(r):
    if "error" in r:
        print(f"Error: {r['error']}")
        return

    prod = r.get('product', 'SPX')
    src = r.get('spot_source', '?')
    exps = r.get('expiries_analyzed', [r.get('expiry', '?')])
    print(f"\n{prod} GEX — 0DTE {r['expiry']} | structural {len(exps)} exp "
          f"({exps[0]}..{exps[-1]}) | {r['regime']} / struct {r.get('structural_regime','?')} | spot {src}")
    print("=" * 78)
    print(f"  Spot: {r['spot']} | ATM straddle: ${r['atm_straddle']} ({r['expected_range_pct']}%) | IV: {r['atm_iv']}%")

    print(f"\n  0DTE view ({r['expiry']}):")
    print(f"    Gamma peak: {r['gamma_peak']['strike']} | flip: {r.get('gamma_flip_0dte')} | regime: {r['regime']}")
    if r.get('put_wall'):
        print(f"    Put wall:   {r['put_wall']['strike']} (OI {r['put_wall']['oi']:,} / vol {r['put_wall']['volume']:,})")
    if r.get('call_wall'):
        print(f"    Call wall:  {r['call_wall']['strike']} (OI {r['call_wall']['oi']:,} / vol {r['call_wall']['volume']:,})")

    print(f"\n  STRUCTURAL view ({len(exps)} expiries):")
    print(f"    Gamma peak: {r.get('gamma_peak_structural',{}).get('strike','?')} | "
          f"flip: {r.get('gamma_flip_structural')} | regime: {r.get('structural_regime')}")
    if r.get('put_wall_structural'):
        pw = r['put_wall_structural']
        print(f"    Put wall:   {pw['strike']} (aggregated OI {pw['oi']:,} / vol {pw['volume']:,})")
    if r.get('call_wall_structural'):
        cw = r['call_wall_structural']
        print(f"    Call wall:  {cw['strike']} (aggregated OI {cw['oi']:,} / vol {cw['volume']:,})")

    print("\n  Per-expiry breakdown:")
    for exp_d, summary in (r.get('levels_by_expiry') or {}).items():
        flip = summary.get('gamma_flip')
        cw = summary.get('call_wall') or {}
        pw = summary.get('put_wall') or {}
        print(f"    {exp_d}: {summary.get('n_strikes',0)} strikes | "
              f"total_gex {summary.get('total_gex',0):+,.0f} | {summary.get('regime','?')} | "
              f"flip {flip} | CW {cw.get('strike','?')} / PW {pw.get('strike','?')}")

    print(f"\n  {'Strike':>7} {'Call γ':>8} {'Put γ':>8} {'C OI':>7} {'P OI':>7} {'C Vol':>7} {'P Vol':>7} {'Net GEX':>10}")
    print(f"  {'-' * 67}")
    top = sorted(r['levels'], key=lambda x: abs(x['net_gex']), reverse=True)[:15]
    for l in sorted(top, key=lambda x: -x['strike']):
        m = "◄" if abs(l['strike'] - r['spot']) < 5 else " "
        print(f"  {l['strike']:>7.0f} {l['call_gamma']:>8.5f} {l['put_gamma']:>8.5f} "
              f"{l['call_oi']:>7,} {l['put_oi']:>7,} {l['call_volume']:>7,} {l['put_volume']:>7,} "
              f"{l['net_gex']:>+10,.0f} {m}")

    if r.get('charm_hotspots'):
        puts = r['charm_hotspots'].get('puts', [])
        if puts:
            print("\n  Charm hotspots (PM decay):")
            put_strs = [f"{p['strike']} (charm {p['charm']:.1f})" for p in puts[:3]]
            print(f"    Puts: {', '.join(put_strs)}")
        calls = r['charm_hotspots'].get('calls', [])
        if calls:
            call_strs = [f"{c['strike']} (charm {c['charm']:.1f})" for c in calls[:3]]
            print(f"    Calls: {', '.join(call_strs)}")


async def main():
    args = sys.argv[1:]
    product = "ES" if "--es" in args else "SPX"

    if "--json" in args:
        r = await compute_gex(product=product)
        print(json.dumps(r, indent=2, default=str))
    elif "--stream" in args:
        while True:
            r = await compute_gex(product=product)
            print_summary(r)
            if product == "SPX":
                r_es = await compute_gex(product="ES")
                es_file = Path(__file__).parent / "gex_levels_es.json"
                atomic_write_json(es_file, r_es)
                print(f"  ES GEX: {r_es.get('spot', '?')} | {r_es.get('regime', '?')}")
            print("\n  Next update in 5 min...")
            await asyncio.sleep(300)
    else:
        r = await compute_gex(product=product)
        print_summary(r)


if __name__ == "__main__":
    asyncio.run(main())
