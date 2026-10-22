# Brief: historical resolved-games dataset + resolution tracking

Written 2026-10-20 by the planning session (`totalis-e0`). Owner: Ruben.
Send a summary to `totalis-e0` when done.

## Why

Two consumers. (1) The joint engine (`rfqbot/pricing`, NFL session) must be validated on
every pair of markets within resolved games: predicted joint probability vs realized joint
outcome, stratified by family pair. Resolved games give tens of thousands of pairs per week,
far more than our own quotes ever will. (2) The backtest needs resolutions for every leg we
quote to compute realized P&L, and `market_resolutions` in `rfq.db` is empty with no writer.

## Verified facts (2026-10-20)

- Gamma marks a resolved market with `closed: true`, `outcomePrices` of exactly `"0"`/`"1"`,
  `umaResolutionStatus: "resolved"`. The parent event's `closed` can still be `false` while
  all its markets are resolved (nfl-sea-den-2026-10-16: 309 markets, all resolved).
- `GET gamma-api.polymarket.com/events?series_slug=nfl-2026&closed=true&limit=N&order=endDate
  &ascending=false` lists closed NFL events newest first, including the per-game side events
  (`-player-props`, `-highest-scoring-quarter`, `-first-td-scorer`). NBA: find the series slug
  from a live NBA market's `events[0].seriesSlug`. Send a browser User-Agent.
- CLOB price history is public: `GET clob.polymarket.com/prices-history?market={token}&startTs=
  {unix}&endTs={unix}&fidelity={minutes}` -> `{"history": [{"t": unix_s, "p": price}, ...]}`
  (187 points at fidelity 10 over ~1.4 days; `interval=1w` also works). It is a mid/last
  series, not a book; good enough for a pre-kickoff snapshot.
- Historical trades are public: `GET data-api.polymarket.com/trades?market={conditionId}&limit=N`
  with side, price, size, timestamp, asset (token). Optional, for a VWAP-style close.
- Kickoff is `gameStartTime` on each market (UTC). Catalog `markets` table has slug, title,
  outcome, position_id; `leg_tokens` in books.db has token ids for anything the recorder saw.

## Deliverables (new package `rfqbot/history/`, you own it; sqlite file `data/history.db`)

1. **Game puller** `python -m rfqbot.history pull --sport nfl --since 2026-09-01`: for every
   closed event of the sport's series (main game event plus its side events), store every
   market: slug, title, outcomes, clob token ids, position ids if the catalog has them,
   `gameStartTime`, `sportsMarketType`, `line`, final `outcomePrices`, resolution status.
   Parse each with the sport's registered Polymarket parser (`rfqbot.sports.registry`) and
   store the `LegSpec` fields, so the engine can consume it. Tables: `hist_games`,
   `hist_markets` (one row per market), `hist_legs` (one per outcome, with `won` 0/1).
2. **Pre-kickoff price snapshots** `python -m rfqbot.history prices --minutes-before 60,15,5`:
   for every token, the last price at or before each offset from kickoff via prices-history
   (fidelity 1 to 5 minutes; be gentle, one request per token, back off on errors; cache so
   reruns are incremental). Table `hist_prices(token_id, offset_min, t, p)`. Record how many
   tokens had no history (illiquid far-ladder lines) so coverage is visible.
3. **Engine-shaped export** `game_markets_hist(game_prefix, offset_min) -> list[MarketQuote]`
   returning the same `rfqbot.ladders.MarketQuote` type with `best_bid = best_ask = p` (sizes
   None) so `rfqbot.pricing.joint.build_game` runs on history unchanged. Coordinate the exact
   import with the NFL session; do not edit its files.
4. **Resolution tracker** `python -m rfqbot.history resolve`: for every distinct market in
   `rfq.db` `rfq_legs` (and all `hist_markets`), fetch Gamma and write resolved outcomes into
   `rfq.db.market_resolutions(condition_id, resolved_outcome, resolved_at_ms)` (table exists;
   write rows only, do not alter the schema, which the NFL session owns). Make it safe to run
   on a timer (only re-check unresolved markets whose `endDate` has passed).
5. **Pair outcome table** `python -m rfqbot.history pairs --sport nfl`: for every resolved game,
   every pair of legs (both outcomes of every market, FG/1H/2H families first; cap per game if
   needed) with `won_a`, `won_b`, family/period/side of each. This is what the engine's
   validation will iterate; it does not call the engine itself.
6. Tests with recorded Gamma / CLOB fixtures (small), parser round trips on historical slugs,
   point-in-time price selection, and incremental caching.

Report back: games and markets pulled per sport, price-snapshot coverage by offset, number of
resolved markets written to `market_resolutions`, pair counts, and any Gamma/CLOB limits hit.
