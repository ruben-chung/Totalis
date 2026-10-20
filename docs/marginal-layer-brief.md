# Brief: marginal layer (fair probability + expected error per leg)

Written 2026-10-20 by the planning session (now `totalis-e0`). Owner: Ruben.
Send a summary to `totalis-e0` when done.

## What it is

For one Polymarket leg at one instant, produce a fair probability and an expected error, from
up to two live quotes: the leg's own Polymarket CLOB book, and the matched Kalshi market's
book when one exists. The design was agreed in planning; this brief makes it concrete. It is
one of four inputs to quote assembly (with the independence classifier, the joint engine and
the inventory layer), so its output shape matters more than its first-cut numbers.

## Inputs (code against these; both producers are in flight)

- Polymarket: `rfqbot.ladders.game_markets(game_prefix, at_ms=None) -> list[MarketQuote]`
  (`totalis-6c`): position_id, token_id, slug, title, outcome, outcome_index, best_bid,
  best_ask, bid_size, ask_size, bid_depth, ask_depth, at_ms, game_start_time. Until it lands,
  read `data/books.db` (`book_top`, `leg_tokens`) directly behind an adapter.
- Kalshi: `rfqbot.kalshi_books.kalshi_for_legs(position_ids, at_ms=None) ->
  dict[position_id, (MatchRow, KalshiQuote | None)]` (`totalis-c8`): MatchRow has
  kalshi_ticker, kalshi_side (yes/no), confidence `exact | rules_differ | shifted_line | none`,
  notes; KalshiQuote has yes_bid, yes_ask, bid_size, ask_size, bid_depth, ask_depth, at_ms.
  Until it lands, synthetic.
- Config in a new `rfqbot/marginals/config.py` (defaults below), read from env by
  `rfqbot/config.py`'s pattern but kept in your package to avoid editing shared files.

## Output

```python
@dataclass(frozen=True)
class Marginal:
    position_id: str
    p: float | None            # fair probability; None when every source is unusable
    err: float | None          # expected absolute error in probability points
    p_poly: float | None; p_kalshi: float | None
    w_poly: float; w_kalshi: float          # normalized precision weights actually used
    kalshi_confidence: str                   # none | exact | rules_differ | shifted_line
    flags: tuple[str, ...]                   # see below; each flag has a defined action
    tier: str                                # ok | wide | thin | stale | disagree | skip
    at_ms: int                               # newest quote time used
    explain: str                             # one line, human readable
```

## Rules

- **Per-venue estimate**: depth-weighted microprice at the touch (bid and ask sizes); plain
  mid when sizes are missing. Kalshi side mapping: if `kalshi_side == "no"`, use `1 - yes`.
- **Per-venue precision**: `1 / sigma_v^2` with `sigma_v = half_spread_v * (1 + c_thin /
  (1 + depth_v / D0)) + c_stale * staleness_v` where staleness is seconds since the quote,
  with a much shorter scale when the game is in play (`game_start_time <= now`). Make
  `c_thin`, `D0`, `c_stale`, and the in-play scale config. Document the formula.
- **Venue prior**: multiply Kalshi's precision by `KALSHI_PRIOR[sport]` (default 2.0 for nfl
  and nba, 1.0 otherwise); these are placeholders the backtest will calibrate.
- **Match confidence**: `exact` combines normally; `rules_differ` combines with Kalshi's
  precision scaled by 0.25 and a flag; `shifted_line` and `none` do not combine (Polymarket
  only; flag `kalshi_unused:<confidence>`). Shifted lines are the joint engine's business later.
- **Combination**: precision-weighted mean; `err` = 1/sqrt(total precision), floored at a
  configurable minimum.
- **Flags and actions** (features flow through sigma; flags are gates or notes, not extra
  error):
  - `wide:<venue>` when spread > `k_wide * p(1-p) + floor`; note only (already in sigma).
  - `thin:<venue>` when depth < `D_min`; note only.
  - `stale:<venue>` when staleness > threshold; the venue is dropped (weight 0) beyond
    `stale_drop` seconds.
  - `inplay` when the game has started; tier at least `stale` unless the quote is < N s old.
  - `disagree` when |p_poly - p_kalshi| > combined half-spreads: do **not** average; take
    the Polymarket value, set `err` to at least the gap, tier `disagree`.
  - `disagree_hard` when the gap exceeds `gap_skip`: tier `skip`, `p` still reported.
  - `degenerate` when p > 0.985 or < 0.015 with a one-sided book: note, `err` floored higher.
  - `one_sided:<venue>` when a side is missing: that venue contributes mid-less (skip it)
    unless it is the only venue, then use the present side with a large sigma and tier `wide`.
  - `no_quote` when nothing is usable: `p = None`, tier `skip`.
- **Fees**: Kalshi quotes are fee-exclusive, Polymarket mids too; no adjustment here. Note it.

## Deliverables

- Package `rfqbot/marginals/` (you own it): `model.py` (dataclasses, rules), `sources.py`
  (adapters to the two quote stores), `config.py`, `__main__.py` with
  `python -m rfqbot.marginals --game nfl-ind-min-2026-10-25 [--at-ms ...]` printing a table
  of legs with p, err, weights, tier, flags, explain; and `--log` to append every computed
  marginal with its features to `data/marginals.db` for later calibration.
- Tests with synthetic quotes for every rule and flag, the Kalshi side mapping, in-play
  staleness, and the point-in-time path (no future quotes). Keep the suite green (164 now).

## Ownership

You own `rfqbot/marginals/*` and `tests/test_marginals_*.py`. Shared files are owned by
others (`totalis-47`: store, __main__, pricing, kalshi client, sports/nfl*; `totalis-6c`:
books, ladders, book config; `totalis-c8`: kalshi_books, clusters, sports/nba*). Not a git
repo: do not edit files you do not own. Ask the owner, or me, for interface changes.

## Report back

The formula and defaults you settled on, the output of the CLI on a real NFL game once books
exist (synthetic until then), and anything that did not fit the rules above.
