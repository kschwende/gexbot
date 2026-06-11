# Pine Script

`gexbot_spx_levels.pine` is the TradingView Pine v6 indicator that draws GEX
levels (put/call walls, gamma flip/peak, volume walls, spot, and the net-GEX
profile) onto a chart.

**This file is a generated example, not the source of truth.** The real
generator is [`gexbot/pine_render.py`](../gexbot/pine_render.py): at runtime it
bakes the *live* GEX frame into the indicator and pushes it onto the local
TradingView chart (see `gexbot/tv_publisher.py`). The data constants near the
top (`SPOT`, `PUT_WALL`, …) change on every refresh; the input toggles and
drawing logic are stable.

This copy was rendered from the bundled sample frame so the indicator is easy
to read, copy/paste into TradingView, or diff. To regenerate it:

```sh
python3 -m gexbot.pine_render --file tests/fixtures/gex_levels_live.sample.json > pine/gexbot_spx_levels.pine
```
