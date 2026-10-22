# Exposure deltas and caps: what was built and what the paper book looks like (2026-10-20)

Built against `claude-sessions/risk-exposure-brief.md` (stage 3 part 1). Package `rfqbot/risk/` (`book.py`,
`exposure.py`, `caps.py`, `cli.py`), CLI `python -m rfqbot risk`, tests `tests/test_risk_*.py`.
Nothing is wired into quoting yet; `Assembler` still prices with `e_i = 0`.

## Shipped

* `rfqbot/risk/book.py`: `PositionLeg(position_id, key, spec, p, game, slug, outcome, condition_id)`,
  `Position(rfq_id, legs, sign, size, price, status, at_ms, expires_ms, clusters, realized)` with
  `pnl(won)`, `notional` (collateral at risk: `price` long, `1 - price` short), `games`, `game_dates`;
  `Book` (`add`, `expire(now_ms)`, `fill`, `settle(resolutions)`, `active()`, `notional_by("sport"|"date")`,
  `summary()`), `Book.from_decisions(decisions_db, assume_filled=True, *, rfq_db=None, now_ms, since_ms,
  until_ms, pending_window_ms=60_000)` and `Book.from_store(store, *, now_ms, p_of=None)`.
  `rfqbot/sports/legspec.py` gained `parse_canonical_key(key, *, venue, rules)` (inverse of
  `canonical_key`) so decisions rows rebuild `LegSpec`s without the catalog; with `rfq_db` the
  catalog row is parsed through the sports registry instead (rules, condition ids, outcomes).
* `rfqbot/risk/exposure.py`: `ModelPool(models | cache=GameCache | build from books)`, `Draws`
  (common random numbers keyed per game / factor), `Exposure(book, models, n_draws=20000, seed)` with
  `game_vec[prefix]` (per-game conditional P&L over the scenarios), `delta(prefix, LegSpec)`,
  `canonical_deltas(prefix)`, `game_distribution(prefix)`, `factor_delta[key]`, `pnl_draws()`,
  `portfolio()`, `derive(positions)` (a candidate on the same models, draws and factors), and
  `report() -> ExposureReport` (`as_dict`, `top_deltas`, `worst_game`).
* `rfqbot/risk/caps.py`: `Caps.from_env()` (`RISK_CAPITAL_USD` 50000, `RISK_MAX_OUTCOME_DELTA` 0.05,
  `RISK_MAX_GAME_WORST_LOSS` 0.10, `RISK_MAX_PORTFOLIO_ES1` 0.15, `RISK_MAX_SPORT_NOTIONAL` /
  `RISK_MAX_DAY_NOTIONAL` off by default; all fractions of capital), `check(book, candidate, models,
  *, caps, exposure) -> CapResult(requested, allowed_size, cap_size, binding_cap, headroom_by_cap,
  detail)`, `marginal_risk(book, candidate, models, *, exposure) -> MarginalRisk` (`d_es1_per_share`,
  `tail_mean_per_share`, `cov_by_game`, `corr_by_game`; `float(mr)` is the ES1 change per share),
  `usage(exposure, book, caps)`.
* CLI `python -m rfqbot risk --from data/decisions.db --assume-filled [--rfq-db] [--books-db] [--at-ms]
  [--game PREFIX] [--outcome KEY] [--n-draws] [--top] [--json PATH]`.
* Tests: `tests/test_risk_book.py` (4), `tests/test_risk_exposure.py` (8), `tests/test_risk_caps.py` (6),
  plus the key round-trip in `tests/test_legspec.py`. Suite: 335 passed.

## How exposure is computed

Each active position is split into independent components: one per game with a `GameModel`
(the product of its legs' indicators over that game's scenarios) and one Bernoulli factor per leg
the engine does not model (NBA, props, unsupported families, NFL games without a model). With
`a = sign * size` and price `q`:

* P&L vector in game g: `a * (ind_g * prod_{c != g} p_c - q)`, the conditional expectation given
  g's scenario with every other component at its probability. Deltas to an outcome O are
  `E[V_g | O] - E[V_g | not O]`; a long moneyline share has delta exactly 1 to its own win.
* Delta to a factor: `a * prod_{c != f} p_c * orient`, orientation -1 for the complement side of the
  same market (over/under, yes/no, the two spread sides), so YES and NO of one market net.
* Portfolio: Monte Carlo with one scenario per game and one uniform per factor (common random
  numbers), every position evaluated exactly, 20k draws. ES1 is minus the mean P&L of the worst
  1% of draws. Tail contributors are first-order attributions (`V_g(draw) - mean V_g`, exact for
  single-game positions).
* Positions with identical components are merged first (110 quotes of one combo = one term), so
  the book's cost is one 50k-scenario pass per distinct combo.

Caps: outcome deltas are linear in size (closed form); per-game worst loss is concave in size and
portfolio ES1 convex (bisection, 60 iterations); notional caps are linear. A cap the book already
breaches does not block a candidate that leaves the measure no worse, and gives 0 to one that
worsens it.

## Numbers: the paper book (decisions 18:42 to 19:02 local, 2026-10-20, assume filled)

| item | value |
|---|---|
| positions | 2,636 active (2,465 NBA, 171 NFL), 2,613 two-leg, 21 three-leg |
| sides | 917 long YES (requester sold), 1,719 short YES (requester bought) |
| shares / collateral at risk | 570,424 / $357,952 |
| distinct combos | 47 |
| modeled games | 12 NFL built from books (5.3 s), 8 NBA games as 29 Bernoulli factors |
| portfolio (20k draws) | mean +7,610, sd 79,615, q01 -172,664, ES1 173,529 = 347% of $50k |

Top five outcomes by dollar delta (all `nfl-ne-chi-2026-10-23`): total over 50.5 -88,704; NE total
over 23.5 -83,751; CHI -3.5 covers +81,983; CHI win +61,305; NE win -61,065. Next: NBA factor
OKC moneyline +55,334 (the OKC@SAS game was in play while the quoter ran).

Worst game: NE@CHI, 116 positions, worst scenario -109,212 (q01 = q05 = worst: the game's P&L is a
step function of a handful of outcomes). Every other game's worst case is under $500.

Cause: one requester sent the same same-game combo (NE +3.5 & over 50.5) 110 times in 20 minutes;
the quoter sold 1,748 shares at 0.286 each time (its $500-per-quote cap), so assume-filled stacks a
154k-share short on one outcome. The caps catch it: on an empty book the first such quote is
allowed at 4,331 shares (outcome cap, `total over 50.5`, 0.577 delta per share against $2,500); on
the current book the allowed size is 0 with `outcome` binding. Marginal risk of one more such quote:
+0.714 ES1 per share (the tail-conditional mean, identical to the finite difference), corr with the
game's book vector 1.0; the hedge (buying the combo) is -0.714 per share.

Timings on this book: load 0.78 s, model builds 5.3 s (0.4-0.8 s per game, once), decomposition
0.02 s, aggregation 0.002 s, report with Monte Carlo 0.25 s; `check()` 14-30 ms and
`marginal_risk()` 1 ms given the book's exposure.

## Corrections to the brief

* `marginal_risk` returns a `MarginalRisk` dataclass, not a float: the brief asks for both the ES1
  change per share and the per-game covariance, and the ES1 change is not a single number (ES1 is
  not differentiable; the finite difference at the candidate's size and the tail-conditional
  derivative at zero are both reported). `float(result)` gives the finite difference.
* The decisions log has no quote deadline, so `assume_filled=False` treats a quote as live for
  `pending_window_ms` (60 s) after its decision; the real deadline is only in `rfq.db` (`from_store`).
* "Delta 1 to its own outcome" for unmodeled legs holds per share times the other components'
  probabilities, as the brief's factorisation implies; the brief's one-line version omits that factor.
* "A moneyline share has delta 1 to its own team's win" holds for the engine's strict-win outcome;
  the delta to the *other* team's win is `-P(mine) / (1 - P(theirs))`, not -1, because ties sit in
  "not O" (0.4% in NFL; visible in the synthetic model without the tie prior).

## Findings

* Repeated RFQs for one combo are the dominant inventory risk in the paper flow; the 110 repeats are
  round-trip style quotes from one requester, all BUY (we sell). The quoter's per-quote size cap is
  no protection; the outcome cap is.
* Per-game P&L vectors are conditional expectations, so they do not sum to the portfolio and a
  cross-game combo's premium appears in every game it touches. Deltas and worst cases are right;
  only the portfolio Monte Carlo is additive.
* NBA legs enter as independent Bernoullis even when two are in the same game (flag
  `unmodeled_same_game_as_independent`; the quoter already skips those combos, so the flag count was
  0 in this book).
* Legs with no marginal (`p` None) are assumed 0.5 and counted in `flags["leg_p_missing_assumed_0.5"]`.
* `Book.from_decisions` keeps the last decision per `rfq_id` (requotes supersede).

## Not done / v2

* Settlement from `market_resolutions` needs condition ids, which only the catalog path provides.
* Tail attribution for cross-game combos is first order; an exact Shapley split is not needed yet.
* The day-notional cap uses the leg's game date (ET for registered sports, slug date otherwise).
* No live refresh: the CLI builds models once from the latest books; the quoting hook will use the
  cache's models through `ModelPool(cache=...)`.
