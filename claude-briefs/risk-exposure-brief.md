# Brief: exposure deltas and caps (stage 3, part 1)

Written 2026-10-20 by the planning session (`totalis-e0`). Owner: Ruben. For the NFL session
(owner of `rfqbot/pricing`, where the scenario sets live). Report back to `totalis-e0`.

## Why

Stage 3 of the spec: track pending and executed exposure across markets, games and the
portfolio, and have quoting widen, skew, reduce or reject as inventory grows. The design settled
in planning: the scenario engine is the factor model, so exposure is measured as deltas to
outcomes computed on the scenario sets, caps are hard limits in layers, and the inventory term
in the margin is the marginal risk a new position adds. This brief covers measurement and caps;
sizing and the inventory skew follow once this exists.

## Definitions (agreed)

- A **position** is a combo (its legs as `LegSpec`s, with side), a direction from the maker's
  point of view (we are long YES if we bought, short YES if we sold), a size in shares and the
  price paid or received. Pending quotes count as positions at their quoted size until they
  expire (status from `rfqs`/`quotes`), executed ones until resolution.
- Within one game, a position's **payoff vector** is its payoff per share in each scenario of
  that game's `GameModel` (indicator product for same-game legs, times sign and size).
  Cross-game combos factorise under the independence certificate: the payoff vector in game g
  is the within-g indicator times the product of the other clusters' probabilities, so every
  position contributes a vector to each game it touches and nothing leaks across games.
- **Delta** of a position (or a book) to an outcome O in a game:
  `delta_O = E[payoff | O] - E[payoff | not O]`, computed on the scenario set. Dollar delta =
  delta times size. A moneyline share has delta 1 to its own team's win.
- Legs the engine does not model (NBA same-game, props, families returning `None`) are their
  own factor: delta 1 to their own outcome, 0 to every other, using the marginal probability.

## Deliverables (`rfqbot/risk/`, you own it; tests `tests/test_risk_*.py`)

1. `book.py`: `Position` and `Book` (positions keyed by rfq_id; add, expire, settle with
   `market_resolutions`), loaders `Book.from_decisions(decisions_db, assume_filled=True)` for the
   paper book and `Book.from_store(store)` for quotes/executions in `rfq.db`.
2. `exposure.py`: per game, the book's P&L vector over scenarios (reusing the quoting cache's
   `GameModel`s; build on demand otherwise) and from it:
   - deltas to the canonical outcomes: each team's win, over/under at the game's main total
     line, cover at the main spread line, each team total over/under at its main line; plus
     delta to any arbitrary `LegSpec` on request;
   - the per-game P&L distribution: mean, sd, worst scenario, 1% and 5% quantiles;
   - the portfolio P&L distribution by Monte Carlo over independent games (sample one scenario
     per game, sum), with mean, sd, 1% expected shortfall, and the games contributing most to it;
   - unmodeled legs aggregated as independent Bernoullis in the same Monte Carlo.
   Output `ExposureReport` dataclass with `as_dict()`; make it cheap (vectorised numpy; a book of
   a few thousand positions across 50 games should take well under a second).
3. `caps.py`: `Caps` config from env (`RISK_CAPITAL_USD` default 50000, `RISK_MAX_OUTCOME_DELTA`
   0.05 of capital per outcome, `RISK_MAX_GAME_WORST_LOSS` 0.10, `RISK_MAX_PORTFOLIO_ES1` 0.15,
   `RISK_MAX_SPORT_NOTIONAL`, `RISK_MAX_DAY_NOTIONAL`) and `check(book, candidate, models) ->
   CapResult(allowed_size, binding_cap, headroom_by_cap)` that returns the largest size of the
   candidate that keeps every cap satisfied (binary search on size is fine; deltas are linear in
   size so most caps solve in closed form). Caps are hard: quoting will never price around them.
4. `marginal_risk(book, candidate, models) -> float`: the change in portfolio 1% expected
   shortfall per share of the candidate, and the covariance between the candidate's payoff
   vector and the book's P&L vector in each game it touches. This is the input to the inventory
   skew term later; do not wire it into quoting yet.
5. CLI `python -m rfqbot risk --from data/decisions.db --assume-filled [--game <prefix>]
   [--outcome <canonical_key>]`: prints the top deltas by outcome, per-game worst cases, the
   portfolio distribution, and which caps would bind. Run it on today's paper decisions log and
   report what the paper book looks like.
6. Tests on synthetic books and synthetic game models: delta of a moneyline is 1 to its own
   win; a cross-game two-leg combo's delta in game g equals the other leg's probability times
   the within-g delta; caps solve the binding size correctly; the portfolio Monte Carlo matches
   the analytic sum for independent single legs; unmodeled legs count as Bernoullis.

## Ownership

You own `rfqbot/risk/*` and `tests/test_risk_*.py` (add them to `.claude/ownership.json` under
`nfl`; that file is planning's, so send me the lines and I will add them, or edit it if the hook
lets you). Not a git repo issue any more, but keep to your files. Suite is 316 green.

## Report back

The paper book's exposure as of the run (top five outcomes by dollar delta, worst game, the
portfolio 1% ES against the 50k), timings, and anything in the definitions that did not hold up.
