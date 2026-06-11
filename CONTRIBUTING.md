# Contributing to gexbot

Thanks for your interest in improving gexbot! This is a small, self-contained
project, so the workflow is light. Please read [ARCHITECTURE.md](ARCHITECTURE.md)
first for the data flow and the two contracts the code is built around.

## Getting set up

```bash
git clone https://github.com/kschwende/gexbot.git
cd gexbot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install ruff      # linter/formatter (not in requirements.txt)
```

You do **not** need market access or TradingView to develop most of the code.
The renderer (`gexbot/pine_render.py`) is pure and runs offline against the
sample fixture — that's where most contributions can be tested end-to-end.

## Development loop

```bash
# 1. Run the renderer unit tests (fast, no market / no TradingView)
.venv/bin/python3 -m unittest tests.test_pine_render

# 2. Render the sample frame and validate the generated Pine source.
#    Static check (offline):
.venv/bin/python3 -m gexbot.pine_render -f tests/fixtures/gex_levels_live.sample.json

#    Server-side compile check (needs TradingView Desktop + tradingview-mcp CLI):
.venv/bin/python3 -m gexbot.pine_render -f tests/fixtures/gex_levels_live.sample.json \
  | node /path/to/tradingview-mcp/src/cli/index.js pine check

# 3. Lint
ruff check .
ruff format .
```

The live path (`gex_stream_service.py`, `tv_publisher.py`) needs a tastytrade
account and a running TradingView Desktop with the CDP debug port open (see the
README). If your change touches those, please describe how you tested it
manually, since they can't run in CI.

## Pull requests

- Branch off `main`, keep the change focused, and open a PR against `main`.
- Run the unit tests and `ruff check .` before pushing — both should pass clean.
- Match the surrounding style: type hints, module docstrings explaining *why*,
  and the existing naming conventions. `ruff.toml` defines the enforced rules.
- If you add a new GEX field or change the Pine output, update the fixture
  (`tests/fixtures/gex_levels_live.sample.json`) and the renderer tests so the
  offline loop stays green.
- For larger or design-level changes, open an issue first so we can agree on the
  approach before you invest the work.

## Reporting bugs

Open a GitHub issue with: what you expected, what happened, the product/expiry
and (if relevant) a minimal `gex_levels_live.json` snippet that reproduces it.
Please **never** paste real credentials, OAuth tokens, or `.env` contents into
an issue.

## Security & secrets

All credentials come from a local `.env` (gitignored — see `.env.example`).
Never commit secrets. If you find a security issue, please report it privately
rather than opening a public issue.

## License

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
