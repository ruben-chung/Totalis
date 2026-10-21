# Brief: backtest and replay (stage 4)

Written 2026-10-21 by the planning session (`totalis-e0`). Owner: Ruben. For session `totalis-48`.
Report back to `totalis-e0`. Builds on `docs/quote-assembly-brief.md` (what a quote is),
`docs/risk-wiring-brief.md` (caps, sizing, inventory skew; landing from the NFL session tonight) and
`docs/margin-optimizer-brief.md` (fill curves, winner's curse, margin table; `rfqbot/edge`, landed).

## Why

Stage 4 of the spec: replay the recorded flow through the quoting path with the books as they were,
fill from the recorded prints, settle from resolutions, and attribute P&L to the margin terms. The
paper listener already logs what we would have quoted live, but it cannot answer the counterfactuals
that decide the design: independence pricing against the joint engine, caps on against off, the fixed
base edge against the fitted margin table, the calibration factors at their fitted values against
one. The backtest is the harness that runs the same handler under each setting over the same four
hours of flow and reports the difference. Evidence for the design choices it tests is in
`docs/engine-validation-notes.md` (engine vs independence), `docs/risk-exposure-notes.md` (repeated
RFQs stacking exposure) and `docs/margin-optimizer-notes.md` (fill curves, markouts).

## Verified facts (checked 2026-10-21 against the repo and the data stores)

- **Raw feed log.** `data/raw/2026-10-20/` (225 MB gz) and `data/raw/2026-10-21/` (192 MB, still being
  written by the recording listener). One JSON object per line: `{"t": ms, "d": "in", "c": conn, "s":
  seq, "p": "<ws frame as a string>"}`. `rfqbot.replay.iter_source(path)` yields rows from a file or a
  directory; `rfqbot.replay.replay(source, store, ...)` loads them into a `Store` (used by `python -m
  rfqbot replay`). First RFQ at 2026-10-20 21:27:33 UTC.
- **rfq.db** (`data/rfq.db`, 1.2 GB, read it with `mode=ro`): 2,353,726 `rfqs` (first_seen_ms
  21:27:33 to 01:52 UTC and growing), 1,588 `trades` (RFQ_TRADE prints: `rfq_id, combo_id, direction,
  side, price_e6, size_e6, executed_at_ms`), `rfqs.status` is FILLED_BY_OTHER for 1,585 and EXPIRED for
  2.36M. `quotes` and `executions` are empty: paper mode never submits, so the only fill evidence is
  the prints. `combos` and `combo_legs(combo_id, leg_index, position_id)` give the legs; `markets`
  maps `position_id` to `condition_id, slug, outcome` for 441,486 positions. `requesters` holds the
  requester ids (the edge package joins them by rfq_id; copy that join).
- **Books.** `data/books.db` `book_top(asset_id, at_ms, server_ts_ms, best_bid, best_ask, bid_size,
  ask_size, bid_depth, ask_depth, source)`: 5,597,793 rows from 21:40:17 UTC. Rows are change-only
  and coalesced to one per token per second; the marginal layer's staleness is confirmation-based
  (`docs/marginal-layer-notes.md`), so an as-of read must use the row with the largest `at_ms <= t`,
  never the next one. `data/kalshi_books.db` `kalshi_top(ticker, at_ms, ..., yes_bid, yes_ask, ...)`:
  2,118,969 rows from 22:35 UTC; `leg_matches` maps Polymarket positions to tickers. Books start 13
  minutes (Polymarket) and 68 minutes (Kalshi) after the first RFQ: RFQs before a game's first book
  row have no fair and must be skipped with their own reason, not priced from a later row.
- **Point-in-time fairs exist.** `rfqbot.marginals.compute_game(game_prefix, at_ms=...)` reads both
  stores as of `at_ms` (`rfqbot/marginals/sources.py: read(game_prefix, at_ms)`); `rfqbot/edge/fairs.py`
  already uses it for later fairs at print + 15 and + 30 minutes. `GameCache` (`rfqbot/quoting/cache.py`)
  takes `poly`, `kalshi` readers and a `now_ms` callable, so the quoting path runs on a pinned clock
  without edits to quoting code. `Assembler.evaluate(rfq, store) -> Decision`;
  `rfqbot/quoting/decisions.py: decision_row(d, config_json)` is the log schema.
- **Decisions log** (`data/decisions.db`, 477,697 rows, 4,764 quoted): `rfq_id, at_ms, direction, side,
  requested_unit, requested_e6, n_legs, legs_json, clusters_json, certified, p_fair, u_m, u_d, e_b, e_i,
  margin, maker_action, price_e6, size_e6, decision, reason, latency_ms, model_built_at, config_json`.
  The NFL session's wiring adds `allowed_size, binding_cap, headroom`, the book summary and the
  pending-count per combo. Skip reasons by count: `leg:sport_not_quoted` 451,687, `leg:unknown_market`
  14,451, `size_below_min` 4,904, `unsupported:nba_same_game` 2,350, `unpriceable_dependence` 1,242.
  Filter on sport before pricing: 95% of RFQs are skipped at the leg parser and cost nothing to replay.
- **Fill rule exists.** `rfqbot/edge/fills.py`: `competitor_gap(direction, p_fair, print_price)`,
  `would_win(direction, p_fair, m, print_price)` (win iff our margin is at or below the gap between the
  print and our fair), `fill_fraction(our_size_e6, print_size_e6)`, `counterfactual(decisions, prints)`.
  Use these; do not write a second fill rule. Its limits (no size priority, no later quotes) are
  recorded in `docs/future-work.md`.
- **Margin table.** `rfqbot/edge/table.py: margin_for(segment, requester=, table=) -> MarginTerms(base,
  curse_mult, info_premium, wallet_premium, blocked, ...)`; `load_table(path, max_age_s)`;
  `rfqbot/edge/margin_table.json` was fitted on the first 8 hours (notes, "First tables"). Fitted
  tables carry `fitted_at_ms`: a replay must not use a table fitted after the RFQ it prices.
- **Risk.** `rfqbot/risk/book.py: Book.add/expire/fill/settle`, `rfqbot/risk/caps.py`,
  `rfqbot/risk/exposure.py` (deltas on the scenario set, ES1). Expiry rule for a pending quote, from the
  listener: `submission_deadline + 10 s acceptance + 1 s last look + EXPIRY_GRACE_MS (20 s default)`.
- **Resolutions are the gap.** `rfq.db.market_resolutions(condition_id, resolved_outcome, resolved_at_ms)`
  has 57,577 rows, all pulled on 2026-10-20; joined through `markets.position_id`, only **31 of the
  1,588 trade prints (26 of 1,385 distinct traded combos on a later count)** have every leg resolved today. `data/history.db` `hist_markets` has 107,663
  resolved markets (NFL 54,290, NBA 53,492) with `outcome_prices`, also keyed by `condition_id`.
  `python -m rfqbot.history resolve --rfq-db data/rfq.db` refreshes `market_resolutions` from Gamma
  (`closed=true` needed; it is a one-shot run, owned by the Data Engineer session). Games in the
  window are NFL week 7 Monday night and NBA opening nights, so most legs resolve over the next days.
  Design the settle step to be re-run; report the unresolved share every time.
- **Units and semantics** (`docs/quote-assembly-notes.md`): combo YES pays 1 pUSD per share iff every
  leg wins; side is always YES; requester BUY means the maker sells YES; `price_e6` is pUSD per share
  for both directions; BUY `requested_size` is a notional budget including fees (prints show
  shares x price / notional of about 0.965), SELL is shares; share size for a BUY at our price is
  `floor(value_e6 * 1e6 / price_e6)`; the venue rejects BUY quotes above 0.90909; SELL prints at 0.999
  are liquidity exits of decided legs, not pricing information.
- **Flow facts to sanity-check against** (`docs/quote-assembly-notes.md`, `docs/margin-optimizer-notes.md`):
  0.135% of requests trade; SELL fills 3.4x BUY; median 133 s from first request to trade; maker
  markouts BUY about 0 to +0.4 pt, SELL -0.2 to -0.6 pt at 15 to 30 minutes.
- **Ownership hook.** Edits by an unregistered session prompt for approval (`.claude/ownership.json`);
  the planning session is adding `rfqbot/backtest/**`, `tests/test_backtest_*.py` and
  `docs/backtest-notes.md` under the label `backtest`. Run `scripts/claim-owner backtest` once.
- **Ruben's rule for realized outcomes**: count evidence in games, not pairs or RFQs; cluster standard
  errors by game; say when a comparison is underpowered rather than reading a sign.

## Deliverables (`rfqbot/backtest/`, CLI `python -m rfqbot.backtest {run|settle|report|compare}`)

1. **Replay harness** `replay.py`: `run(source: Path, *, start_ms, end_ms, cfg: BacktestConfig,
   out_db: Path) -> RunSummary`. Iterate RFQ_REQUEST frames from the raw log in time order; pin a
   clock to the frame's `t`; build a `GameCache` whose `poly` and `kalshi` readers read the two book
   stores as of `t` (largest `at_ms <= t`, same staleness rules as live; no row after `t` may be
   read, test it); run `Assembler.evaluate` with the live `QuoteConfig` and the stage 3 caps `Book`;
   add pending positions at quoted size, expire them by the listener's rule, fill them from the
   prints (deliverable 2), so later quotes see the book the live bot would have had. The margin
   table is the one fitted before `start_ms`, or the default edge if none. Write decision rows in the
   `decisions.db` schema plus `run_id` and `fill_*` columns to `out_db` (default `data/backtest.db`;
   never write to the live stores). `BacktestConfig` carries every switch in deliverable 5 and is
   stored as JSON with the run. Skip reasons for "no book yet" and "book older than the staleness
   bound" are distinct from the live reasons.
2. **Fills** `fills.py`: from `rfq.db.trades` by rfq_id through `rfqbot.edge.fills`; record the
   win flag, the fraction, and a second **strict** flag (our price better than the print by at least
   one tick) so the report shows the range. A filled position's size is the fraction times our size.
3. **Settlement** `settle.py`: for every filled combo, resolve each leg from `market_resolutions`
   then `hist_markets` by `condition_id`; combo YES wins iff every leg resolves YES; voids and 50-50
   resolutions are flagged and excluded with a count; unresolved combos are marked to the last model
   fair in the window and reported as unrealized, separately from realized. Re-runnable
   (`python -m rfqbot.backtest settle --run <id>` after the Data Engineer's resolve refresh).
4. **Report** `report.py`: per run, (a) requests seen, priced, quoted, filled (both fill flags) by
   direction, sport, n_legs and pair class; (b) gross markup earned (price minus fair at fill),
   realized P&L on the resolved subset, mark-to-model P&L on the rest, with game-clustered standard
   errors and the number of distinct games behind each number; (c) P&L attribution: for each fill,
   `payoff - price = (payoff - fair) + (fair - price)`, the first term is pricing error, the second
   is the margin, split further into base, u_m, u_d, e_i, curse, info and wallet premiums as logged;
   (d) markouts of our fills at 1/5/15/30 minutes on the model fair (reuse `rfqbot.edge.markout`);
   (e) the ES1 path over the window (max, time at max), caps binding counts and shares cut per cap;
   (f) latency p50/p99 of the replayed handler. Plain-text output and a JSON file per run.
5. **Compare** `compare.py`: the same window under each switch, one `run_id` per setting, a table
   of fills, markup, realized and mark-to-model P&L, max ES1: joint engine vs independence pricing;
   calibration factors fitted vs all 1 vs scaled 0.75; caps on vs off; base edge 0 / 0.5 / 1 / 2 pt
   vs the margin table; shock set x0.5 / x2 for caps. State for each comparison whether the
   difference exceeds its clustered standard error.
6. **Tests** `tests/test_backtest_*.py`: a deterministic replay over the 75 s sample in the repo with
   a synthetic book fixture; the no-lookahead test (inserting a book row at `t + 1` must not change
   the quote at `t`); settle with a void leg and with an unresolved leg; attribution sums to the
   total; compare runs produce one row per setting.

Out of scope: live submission; hedging; walk-forward refits of the margin table inside the backtest
(one table per run); soccer and other sports the quoter does not price; any change to pricing,
quoting, risk or edge code (message the owner if an interface is missing, see below).

Run-time budget: 2.35M requests at ~1 ms each is 40 minutes; with the sport filter before pricing it
should be a few minutes per run. Run against the stores read-only (`mode=ro`, as in
`rfqbot/pricing/validate.py: _ro`) while the recorders keep writing to them.

## Ownership and coordination

You own `rfqbot/backtest/**`, `tests/test_backtest_*.py`, `docs/backtest-notes.md`. Do not edit
`rfqbot/quoting/**`, `rfqbot/risk/**`, `rfqbot/pricing/**` (NFL session), `rfqbot/edge/**` (NBA
session), `rfqbot/books.py`, `rfqbot/history/**` (Data Engineer), `rfqbot/marginals/**` (yours
already, under the props label; keep changes there separate). If `GameCache` or `Assembler` needs
a hook you cannot inject from outside, message `NFL` with the exact signature you want; for the
edge table or fill helpers message `NBA`; for a resolve refresh message `Data Engineer`. The NFL
session's wiring commit lands tonight; build the harness against the current `Assembler.evaluate`
and pick up the new decision columns when they land (they are additive). Run
`scripts/claim-owner backtest` once before editing. Use `scripts/run.sh` for anything long-running.

## Report back

Send `totalis-e0`: requests replayed and the window; quoted and filled counts under both fill flags;
gross markup, realized P&L with the number of resolved combos and games behind it, mark-to-model
P&L; the attribution table; markouts of our fills; max ES1 and which caps bound; the compare table
with clustered standard errors; replay run time. Say plainly if anything above turned out wrong,
in particular if the as-of book readers cannot reproduce the live fairs (compare the replayed
`p_fair` to the live `decisions.db` row for the same rfq_id; they should agree to the staleness
bound) and if the raw log is missing frames the live listener saw. Write
`docs/backtest-notes.md` with the `/report` skill when done.
