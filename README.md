# rfqbot: Polymarket Combos RFQ paper-trading bot

Python 3.11. Listens to Polymarket's combos RFQ feed, records it, prices each combo from live
Polymarket and Kalshi books with an explicit dependence model for same-game legs, sizes against
exposure caps, and logs the paper quote. Spec: `polymarket-rfq-bot-spec.md`.

## Status

| stage | state |
|---|---|
| 1. Listener and storage | done: ws client, verbatim raw log, SQLite store, derived expiry, replay |
| 2. Pricing and correlation | done, paper mode: NFL/NBA parsers and Kalshi matching, ladder books, marginal layer, independence classifier, NFL joint engine with validation and calibration, quote assembly |
| 3. Inventory and risk | done: outcome deltas, hard caps, Kelly sizing, inventory skew, fitted margin table, wallet blocks |
| 4. Backtest | in progress: `rfqbot/backtest` (brief `docs/backtest-brief.md`) |

Backlog and ideas: `docs/future-work.md`.

## Setup

```bash
uv venv .venv && uv pip install -e ".[dev]" --python .venv/bin/python
cp .env.example .env            # public feeds need no credentials
.venv/bin/python -m pytest -q   # ~375 tests; three files skip without data/
```

## Run

```bash
scripts/run.sh rfqbot listen --quote   # RFQ listener + paper quotes -> data/rfq.db, data/raw/, data/decisions.db
scripts/run.sh rfqbot.books            # Polymarket leg books and game ladders -> data/books.db
scripts/run.sh rfqbot.kalshi_books     # Kalshi top-of-book and leg matches -> data/kalshi_books.db
scripts/run.sh rfqbot.edge monitor     # margin table refit every 5 min -> rfqbot/edge/margin_table.json
scripts/status.sh                      # running rfqbot processes
```

`scripts/run.sh` starts a module once, detached under `caffeinate`, log in `data/logs/`. One-shot
commands: `.venv/bin/python -m rfqbot --help` (listen, catalog, replay, stats, bookstats,
kalshi-sync, coverage, joint, joint-validate, joint-calibrate, risk) and module entry points
`rfqbot.history`, `rfqbot.props`, `rfqbot.marginals`, `rfqbot.edge`, `rfqbot.sports.nba_coverage`.
`PAPER_TRADING=false` is rejected: no signing path.

## Architecture

```
RFQ_REQUEST -> normalizer -> store (rfqs, combos, combo_legs)
  legs        catalog rows (position_id, condition_id, slug)
  clusters    same_market | same_group | same_game | no_game_key | shared_team_diff_game | disjoint
  marginals   Polymarket book_top + Kalshi kalshi_top -> precision-weighted p, error, tier
  dependence  same-game NFL -> joint score engine (ladders -> IPF -> 50k scenarios) -> p_joint, calibrated
              cross-game -> product; same_market/same_group -> 0; no_game_key -> refused
  margin      terms from margin_table (base, curse, info, wallet) x (u_m + u_d) + inventory skew e_i
  size        min(requested, tier cap, Kelly, cap headroom); caps: outcome 5%, game 10%, ES1 15% of capital
PaperQuote -> store.quotes, raw log, data/decisions.db (every RFQ, every term, quoted or not)
```

`Listener(on_rfq=handler)` is the seam: handler gets `RfqRequest` and `Store`, returns `PaperQuote` or `None`.

## Components

| module | does | docs |
|---|---|---|
| `ws_client, listener, messages, normalizer, store, rawlog, replay, catalog` | gateway client, lifecycle, store, raw log, replay, catalog | this file |
| `books.py, ladders.py` | CLOB recorder for RFQ legs and whole-game ladders, point-in-time reads | `ladder-collector-brief.md` |
| `sports/` | venue market -> `LegSpec`; `matcher.match` -> exact / rules_differ / shifted_line / none | `nfl-matching-notes.md`, `nba-matching-notes.md` |
| `kalshi.py, kalshi_books.py` | Kalshi client, catalog, live top-of-book, `leg_matches` | `kalshi-feed-brief.md` |
| `marginals/` | per-leg fair and error from both venues | `marginal-layer-notes.md` |
| `clusters.py` | independence classifier | `independence-classifier-brief.md` |
| `pricing/` | NFL joint engine, indicators, shock interval, validation, calibration | `joint-engine-notes.md`, `engine-validation-notes.md` |
| `quoting/` | semantics, margin, size, game cache, live inventory, decisions log | `quote-assembly-notes.md`, `risk-wiring-notes.md` |
| `risk/` | outcome deltas, caps, marginal ES1 | `risk-exposure-notes.md` |
| `edge/` | fill curves, winner's curse, markouts, wallet scores, margin table | `margin-optimizer-notes.md` |
| `history/` | resolved games, pre-kickoff prices, pairs, `market_resolutions` | `history-dataset-brief.md` |
| `props/` | NBA prop dataset and share-plus-residual fit (not wired) | `props-fit-notes.md` |
| `backtest/`, `probe/` | replay harness; probe RFQs (dry-run) | `backtest-brief.md`, `probe-rfq-brief.md` |

## Data

`data/` is gitignored. Feed facts (2026-10-20): public ws streams `RFQ_REQUEST`, `RFQ_TRADE`,
`RFQ_UNMET_DEMAND_SUMMARY` to any connection; ~190 RFQ/s; 0.135% of requests trade; no expiry
message, so expiry = `submission_deadline + 10 s + 1 s + EXPIRY_GRACE_MS`. Combo YES pays 1 iff
every leg wins; side is always YES; BUY means the maker sells YES; BUY above 0.90909 is rejected.

| store | content |
|---|---|
| `data/raw/YYYY-MM-DD/HH.jsonl.gz` | every frame verbatim, ~3 GB/day |
| `data/rfq.db` | rfqs, combos, combo_legs, requesters, trades, markets (catalog), kalshi_markets, market_resolutions |
| `data/decisions.db` | one row per RFQ seen by `listen --quote` |
| `data/books.db` | `book_top` change-only top of book per CLOB token; position_id is not a token id, mapped via Gamma |
| `data/kalshi_books.db` | `kalshi_top` change-only, `leg_matches` |
| `data/history.db` | `hist_games/markets/legs/prices/pairs` from Gamma (`closed=true` for resolved) |
| `data/edge.db`, `data/probe.db`, `data/backtest.db` | margin optimiser state, probes, backtest runs |

Both book stores are change-only; staleness is measured from the feed's last confirmation.

## Docs

`docs/*-brief.md` is how a task was framed and what was verified first; `docs/*-notes.md` is what
was built and what the data said. `docs/future-work.md` is the backlog; `docs/coupling-environment-study.md`
an idea recorded and not developed.

## Workflow

Built by one planning session and peer Claude Code sessions on one checkout. `/brief` and `/report`
skills fix the shape of delegation and completion. `.claude/hooks/ownership.py` asks before a
session edits a file it does not own (`.claude/ownership.json`, `scripts/claim-owner <label>`);
`.claude/hooks/run_tests.py` runs the matching tests after each edit; `scripts/run.sh` keeps one
instance per process.
