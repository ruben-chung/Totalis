# rfqbot

Paper-trading bot for Polymarket Combos RFQs. Spec in `instructions.md`, results and next steps in `Summary.md`.

## Setup

```bash
uv venv .venv && uv pip install -e ".[dev]" --python .venv/bin/python
cp .env.example .env            # public feeds need no credentials
.venv/bin/python -m pytest -q   # ~490 tests
```

## Run

```bash
scripts/run.sh rfqbot listen --quote   # RFQ listener + paper quotes -> data/rfq.db, data/decisions.db
scripts/run.sh rfqbot.books            # Polymarket leg books and game ladders -> data/books.db
scripts/run.sh rfqbot.kalshi_books     # Kalshi books and leg matches -> data/kalshi_books.db
scripts/run.sh rfqbot.edge monitor     # margin table refit every 5 min -> data/margin_table.json
.venv/bin/python -m rfqbot.backtest compare --group g1 --start 2026-10-21T02:36 --end 2026-10-21T15:45
```

`python -m rfqbot --help` lists the one-shot commands. Everything runs in paper mode; nothing is submitted.

## Layout

`rfqbot/` is the package (listener and store; `sports/`, `marginals/`, `clusters.py`, `pricing/` for fairs and dependence; `quoting/`, `risk/`, `edge/` for quotes, caps and margins; `backtest/`; `probe/`). `claude-sessions/` holds the briefs and notes exchanged between the Claude Code sessions that built it.
