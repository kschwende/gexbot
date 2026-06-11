"""Offline tests for the Pine renderer — no TradingView/market needed.

Run:  python3 -m pytest tests/test_pine_render.py    (or: python3 -m unittest)
"""

import json
import unittest
from pathlib import Path

from gexbot.pine_render import render_pine

FIXTURE = Path(__file__).parent / "fixtures" / "gex_levels_live.sample.json"


class TestPineRender(unittest.TestCase):
    def setUp(self):
        self.gex = json.loads(FIXTURE.read_text())
        self.src = render_pine(self.gex)

    def test_is_v6_indicator(self):
        self.assertTrue(self.src.startswith("//@version=6"))
        self.assertIn("indicator(", self.src)
        self.assertIn("overlay=true", self.src)

    def test_bakes_key_levels(self):
        # Each headline level appears as a Pine constant with the fixture value.
        self.assertIn("SPOT          = 6912.45", self.src)
        self.assertIn("PUT_WALL      = 6850.00", self.src)
        self.assertIn("CALL_WALL     = 6950.00", self.src)
        self.assertIn("GAMMA_FLIP    = 6895.00", self.src)
        self.assertIn("GAMMA_PEAK    = 6900.00", self.src)

    def test_toggles_present_and_stable(self):
        # Stable input titles are the contract that lets TradingView preserve a
        # user's toggle choices across a data refresh.
        for title in ('"Put / Call walls (OI)"', '"Gamma flip"', '"Gamma peak"',
                      '"Spot"', '"Net-GEX profile (right edge)"', '"Labels"',
                      '"Auto-anchor SPX → chart price"'):
            self.assertIn(title, self.src)

    def test_profile_arrays_match_levels(self):
        n = len([lv for lv in self.gex["levels"]
                 if lv.get("strike") is not None and lv.get("net_gex") is not None])
        # array.from(...) for both strike and net-gex profile arrays.
        self.assertIn("PROF_STRIKE = array.from(", self.src)
        self.assertIn("PROF_NETGEX = array.from(", self.src)
        # One strike token per level (commas = n-1 inside array.from).
        prof_line = next(ln for ln in self.src.splitlines()
                         if ln.startswith("var PROF_STRIKE"))
        self.assertEqual(prof_line.count(",") + 1, n)

    def test_missing_levels_render_na(self):
        # A frame with no walls/flip must still produce valid 'na' constants,
        # not crash or emit None.
        sparse = {"spot": 6900.0, "product": "SPX", "expiry": "2026-06-11",
                  "regime": "POSITIVE_GAMMA", "levels": []}
        src = render_pine(sparse)
        self.assertIn("PUT_WALL      = na", src)
        self.assertIn("CALL_WALL     = na", src)
        self.assertIn("PROF_STRIKE = array.new<float>()", src)
        self.assertNotIn("None", src)

    def test_string_fields_escaped(self):
        # A quote in a string field must not break the Pine string literal.
        g = dict(self.gex, regime='WEIRD"REGIME')
        src = render_pine(g)
        self.assertIn('WEIRD\\"REGIME', src)


if __name__ == "__main__":
    unittest.main()
