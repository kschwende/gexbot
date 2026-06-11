#!/usr/bin/env python3
"""
TV Publisher — push the GEX overlay onto the local TradingView chart
=====================================================================
Replaces the old ``chart_feed_service`` + Cloudflare path. Instead of serving
JSON to a remote website, this headless daemon:

  1. reads the live GEX frame (``gex_levels_live.json``, written by the
     ``gex-stream`` daemon every ~12s),
  2. renders it into a self-contained Pine v6 indicator
     (``gexbot.pine_render.render_pine``),
  3. pushes that source onto the running TradingView Desktop chart by shelling
     out to the local TradingView MCP CLI (``tv pine set`` + ``tv pine compile``),
     which drives TradingView over the CDP debug port (9222).

The indicator carries ALL the data; *what shows* is controlled by native
``input.bool`` toggles in TradingView's indicator settings. Those toggle values
survive a refresh because the input structure is stable across renders — only
the baked data constants change.

It re-pushes only when the displayed levels actually move (a content signature),
with a slow heartbeat so the spot line/status still tick over in quiet tape.

Config (env)
------------
  GEXBOT_DATA_DIR        where gex_levels_live.json lives (default <repo>/data)
  GEXBOT_TV_CLI          path to the MCP CLI entry (default ~/tradingview-mcp/src/cli/index.js)
  GEXBOT_TV_NODE         node binary (default "node")
  GEXBOT_TV_REFRESH_SEC  poll/refresh cadence in RTH (default 30)
  GEXBOT_PINE_OUT        where to write the generated .pine (default <data>/gex_overlay.pine)

Run::

    python3 -m gexbot.tv_publisher
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from gexbot.pine_render import render_pine

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logger = logging.getLogger("tv_publisher")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s tv_publisher %(levelname)s %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

ET = ZoneInfo("America/New_York")
DATA_DIR = Path(os.environ.get("GEXBOT_DATA_DIR",
                               Path(__file__).resolve().parent.parent / "data"))
GEX_LIVE_PATH = DATA_DIR / "gex_levels_live.json"
GEX_PATH = DATA_DIR / "gex_levels.json"  # 15-min fallback if the daemon is down

TV_CLI = os.path.expanduser(
    os.environ.get("GEXBOT_TV_CLI", "~/tradingview-mcp/src/cli/index.js"))
TV_NODE = os.environ.get("GEXBOT_TV_NODE", "node")
REFRESH_SEC = int(os.environ.get("GEXBOT_TV_REFRESH_SEC", "30"))
PINE_OUT = Path(os.environ.get("GEXBOT_PINE_OUT", DATA_DIR / "gex_overlay.pine"))

HEARTBEAT_SEC = 120  # re-push at least this often during RTH even if levels are flat
DOWN_BACKOFF_SEC = 300    # retry cadence while TradingView/CDP is unreachable
DOWN_REMINDER_SEC = 1800  # re-log "still unreachable" at most this often
RTH_OPEN = dtime(9, 30)
RTH_CLOSE = dtime(16, 0)

# Substrings in CLI output that mean TradingView/CDP is unreachable (port closed
# → ECONNREFUSED; app quit / no chart → "No TradingView chart target") as opposed
# to a genuine Pine compile error. Used to pick a slow vs fast retry.
_UNREACHABLE_MARKERS = (
    "No TradingView chart target",
    "ECONNREFUSED",
    "ECONNRESET",
    "ETIMEDOUT",
    "WebSocket",
)


def _classify(text: str) -> str:
    """'unreachable' if the failure text smells like a CDP/connection problem,
    else 'error' (a real compile or logic failure)."""
    return "unreachable" if any(m in (text or "") for m in _UNREACHABLE_MARKERS) else "error"


def _is_rth(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    t = now_et.timetz().replace(tzinfo=None)
    return RTH_OPEN <= t <= RTH_CLOSE


def _load_frame() -> dict | None:
    """Read the freshest GEX frame: live file preferred, 15-min file as fallback.
    Returns None when neither is present/parseable."""
    for path in (GEX_LIVE_PATH, GEX_PATH):
        try:
            if path.exists():
                return json.loads(path.read_text())
        except Exception as exc:
            logger.warning("failed reading %s: %s", path.name, exc)
    return None


def _signature(gex: dict) -> tuple:
    """A hashable summary of the *displayed* levels. Spot is rounded to 1pt so a
    drifting tape doesn't force a push every poll, while wall/flip/peak moves
    (on the strike grid) always register."""
    def s(v):
        return v.get("strike") if isinstance(v, dict) else v

    spot = gex.get("spot")
    prof = tuple((lv.get("strike"), round((lv.get("net_gex") or 0) / 1e9, 2))
                 for lv in (gex.get("levels") or []))
    return (
        round(spot) if isinstance(spot, (int, float)) else None,
        s(gex.get("put_wall")), s(gex.get("call_wall")),
        s(gex.get("put_volume_wall")), s(gex.get("call_volume_wall")),
        s(gex.get("gamma_flip")), s(gex.get("gamma_peak")),
        gex.get("regime"), prof,
    )


def _run_cli(*cli_args: str, input_text: str | None = None) -> dict:
    """Invoke the TradingView MCP CLI and parse its JSON result. Raises
    subprocess.CalledProcessError on non-zero exit so the loop can back off."""
    proc = subprocess.run(
        [TV_NODE, TV_CLI, *cli_args],
        input=input_text, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cli_args, output=proc.stdout, stderr=proc.stderr)
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"raw": proc.stdout.strip()}


def _push(gex: dict) -> str:
    """Render → write .pine → set source → compile.

    Returns one of:
      "ok"           clean compile, overlay updated
      "unreachable"  TradingView/CDP is down — caller should back off slowly
      "error"        compile error or other failure — caller short-backs off
    """
    source = render_pine(gex)
    PINE_OUT.parent.mkdir(parents=True, exist_ok=True)
    PINE_OUT.write_text(source)

    try:
        set_res = _run_cli("pine", "set", "--file", str(PINE_OUT))
        if set_res.get("success") is False:
            return _classify(json.dumps(set_res))
        res = _run_cli("pine", "compile")
    except subprocess.CalledProcessError as exc:
        return _classify((exc.stderr or "") + (exc.output or ""))
    except subprocess.TimeoutExpired:
        return "unreachable"  # CLI hung waiting on CDP — treat as down
    except FileNotFoundError as exc:
        logger.error("node not found (%s) — set GEXBOT_TV_NODE to its full path", exc)
        return "error"

    if res.get("success") is False:
        return _classify(json.dumps(res))
    errs = res.get("error_count", 0) or 0
    if errs:
        logger.error("Pine compiled with %d error(s): %s", errs, res.get("errors") or res)
        return "error"
    logger.info("pushed GEX overlay — spot %s, regime %s",
                gex.get("spot"), gex.get("regime"))
    return "ok"


def main() -> None:
    logger.info("tv_publisher: starting (cli=%s, refresh=%ss)", TV_CLI, REFRESH_SEC)
    last_sig = None
    last_push = 0.0
    backoff = 2.0           # short exponential backoff for genuine errors
    tv_down = False         # currently believe TradingView/CDP is unreachable
    down_since = 0.0
    last_down_log = 0.0

    while True:
        now_et = datetime.now(ET)
        gex = _load_frame()

        if gex is None or gex.get("error"):
            # No frame yet (gex-stream still warming, or off-hours with no file).
            time.sleep(REFRESH_SEC if _is_rth(now_et) else 300)
            continue

        sig = _signature(gex)
        age = time.time() - last_push
        changed = sig != last_sig
        heartbeat = _is_rth(now_et) and age >= HEARTBEAT_SEC

        if not (changed or heartbeat or last_sig is None):
            # Nothing new to draw — idle until the next poll.
            time.sleep(REFRESH_SEC if _is_rth(now_et) else 300)
            continue

        status = _push(gex)

        if status == "ok":
            if tv_down:
                logger.info("TradingView reachable again — resumed pushing")
                tv_down = False
            last_sig = sig
            last_push = time.time()
            backoff = 2.0
            time.sleep(REFRESH_SEC if _is_rth(now_et) else 300)

        elif status == "unreachable":
            # TradingView is quit or running without the CDP debug port. Retry
            # slowly and log at most once per DOWN_REMINDER_SEC so a multi-day
            # outage costs almost nothing and the log stays readable.
            now = time.time()
            if not tv_down:
                tv_down, down_since, last_down_log = True, now, now
                logger.warning(
                    "TradingView unreachable on CDP 9222 — is it running with "
                    "--remote-debugging-port=9222? Retrying every %ds.", DOWN_BACKOFF_SEC)
            elif now - last_down_log >= DOWN_REMINDER_SEC:
                last_down_log = now
                logger.warning("TradingView still unreachable (%d min).",
                               int((now - down_since) / 60))
            time.sleep(DOWN_BACKOFF_SEC)

        else:  # "error" — real compile/other failure: short exponential backoff
            logger.warning("push failed (compile/other) — retrying in %.0fs", min(backoff, 60.0))
            time.sleep(min(backoff, 60.0))
            backoff = min(backoff * 2, 60.0)


if __name__ == "__main__":
    main()
