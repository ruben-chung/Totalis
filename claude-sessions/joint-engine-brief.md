# Brief: NFL market-implied joint distribution engine (same-game dependence)

Written 2026-10-20 by the planning session (now `totalis-4d`, formerly `totalis-94`).
Owner: Ruben. When done, send a summary back to `totalis-4d` via SendMessage.
Builds on your NFL matching layer (`LegSpec`, `registry`, `nfl_poly`).

## The idea

Every market in a game is a yes/no function of the scores. If we hold a probability
distribution over score outcomes for a game, the joint probability of any set of legs is the
mass of outcomes where all of them are true. Dependence is not estimated per pair; it falls
out of the shared score variables. Polymarket lists deep ladders per NFL game (17 spread lines
per side, 21 totals, 11 team-total lines per team, 16 1H spread lines per side, 5 1H totals,
6 1Q totals), and a spread ladder at half-point lines is exactly the market's distribution of
the integer margin: `P(M = k) = S(k - 0.5) - S(k + 0.5)`. So the engine's job is:

1. read each ladder into a discrete distribution (margin, total, each team's points, and the
   same for the first half),
2. combine them into one joint distribution over `(home_pts, away_pts)` per period,
3. sample a scenario set from it,
4. evaluate any `LegSpec` as an indicator over the scenarios, and
5. report, for a set of legs, the joint probability with an honest interval.

Pricing rules, margins, quoting and other sports are out of scope. Player props are out of
scope (NFL has only team yards/TDs, which are not score-derived; the engine reports them as
unsupported).

## Inputs

A pure function of one game's market quotes. The quotes come from `rfqbot.ladders.game_markets(
game_prefix, at_ms=None) -> list[MarketQuote]` (being built by `totalis-6c`, brief in
`claude-sessions/ladder-collector-brief.md`; fields: position_id, token_id, slug, title, outcome,
outcome_index, best_bid, best_ask, bid_size, ask_size, bid_depth, ask_depth, at_ms,
game_start_time). Until it lands, code against that signature and test with synthetic quotes.
Parse each quote to a `LegSpec` with `nfl_poly.parse_poly_nfl`. For v1 derive the fair
probability and an error per leg inside the engine with a simple, documented rule (mid of
best bid/ask; error = half-spread plus a thin-book term; None if a side is missing); the real
marginal layer will replace this later, so keep it behind one function.

## Construction

**Ladders.** Group legs by (family, period, subject). Orient everything to one convention:
home margin `M = H - A`, total `T = H + A`, home points `H`, away points `A`. A spread leg
"team X -7.5 / side X" is `P(M_X > 7.5)`; both-side markets at the same line and both
outcomes of each market are separate observations of the same survival curve. Fit each
ladder's survival function by weighted isotonic regression (weights `1/err^2`), clipped to
[0,1]. Report violations: an observation more than `k * err` away from the monotone fit is a
flag on that leg (stale or free money) and is down-weighted, not silently smoothed. Convert the
fitted survival at half-point lines into a pmf on integers. Beyond the ladder, extend with a
parametric tail anchored to the fit (normal; default NFL scales margin 13.5, total 10.5, used
only outside the listed lines and as the prior below). Score support: `H, A` in `[0, 80]`.

**Joint over (H, A) per period.** Fit a 2-D table on the integer grid by iterative
proportional fitting to four 1-D targets: the pmfs of `M`, `T`, `H`, `A`. Start from a prior
table (discretized bivariate normal with the default scales and zero correlation, centred on
the ladders' means). The team-total ladders are what pin the margin-total coupling, so when
they are missing the prior's correlation (zero) carries it; say so in the output notes. IPF
over a 81x81 grid with four marginals converges in a few dozen sweeps; stop on L1 change
below 1e-6. Mass with `M` and `T` of different parity is impossible on an integer grid and the
grid handles that by construction.

**Periods.** Build the first-half table the same way from the 1H ladders (margin 16 lines per
side, totals 5 lines, team totals if present; otherwise prior scaled to a half). Couple halves by
independent increments: second-half scores are `FG - 1H`, and the pair `(1H, FG)` is drawn with
a Gaussian copula per component whose correlation is `sigma_1H / sigma_FG`, then clamped so
second-half points are non-negative (count and report the clamp rate; above 2% is a sign the
ladders disagree). Quarters are v2; v1 supports `FG`, `1H`, `2H`.

**Overtime.** Full-game ladders include overtime. v1 treats the FG table as final including
OT and does not model regulation separately; `exact_margin` with bucket `tie` is the only FG
family that needs a tie, and the FG margin pmf at 0 carries it. Document this.

**Scenarios.** Draw `N = 50_000` scenarios of `(H1, A1, H, A)` as int16 numpy arrays per
game. Rebuild when any input quote changes materially (debounce 5 s) and record `built_at`,
input quote timestamps, convergence stats and flags on the result object.

**Indicators.** `indicator(spec: LegSpec, scen) -> np.ndarray[bool] | None` for: moneyline
(FG, 1H, 2H; tie handling per the spec's rules: `side="tie"` or 50-50 is a settlement matter,
expose `P(tie)` separately), spread, total, team_total, btts (both teams score > 0 in the
period), exact_margin (home/away bands as parsed), highest_scoring_quarter -> None in v1,
safety / two_pt_conversions / team_tds / team_off_yards -> None (not score-derived). `None`
means "not modeled", and the caller falls back.

**Joint quote.** `joint(specs: list[LegSpec]) -> JointQuote` with fields: `p_joint`,
`p_independent` (product of the legs' own fair probabilities), `frechet_lo`, `frechet_hi`,
`lo`, `hi` (the engine's interval, see below, always clipped to the Fréchet bounds),
`p_legs` (the engine's own marginal for each leg, to be compared with the input fair prob),
`max_marginal_error` (largest |engine marginal - input fair prob| across the legs; the engine
must reproduce its inputs, and this number is the proof), `unsupported` (legs returning None),
`flags`, `n_scenarios`, `built_at`.

**Interval.** Recompute `p_joint` under a small shock set and take min/max: (a) halves
coupling correlation +/- 0.15, (b) prior correlation for the margin-total coupling at
-0.2 and +0.2 when team-total ladders are missing or thin, (c) two parametric-bootstrap
redraws of the ladders within their errors. Keep the shock set small and fixed so results are
reproducible; expose it as a parameter for the backtest's "sensitivity to correlation".

## Deliverables

- `rfqbot/pricing/__init__.py`, `rfqbot/pricing/ladders_fit.py` (isotonic fit + pmf),
  `rfqbot/pricing/joint_nfl.py` (IPF, periods, scenarios), `rfqbot/pricing/indicators.py`,
  `rfqbot/pricing/joint.py` (public API: `build_game(quotes) -> GameModel`,
  `GameModel.joint(specs)`, `GameModel.correlation_table()` for diagnostics).
- CLI `python -m rfqbot joint --game nfl-ind-min-2026-10-25 [--at-ms ...] [--pairs]` that builds
  from `ladders.game_markets`, prints the marginal-consistency check, the flags, and a table of
  pairwise joint vs independent probabilities for the main families. You own `__main__.py`;
  `totalis-6c` may send you snippets for its own subcommands.
- numpy is fine to add to `pyproject.toml`; avoid scipy unless isotonic regression without it
  is painful (pool-adjacent-violators is 30 lines).
- Tests: synthetic truth recovery (generate quotes from a known bivariate normal with
  correlation 0.3 and team-total ladders, check IPF recovers the correlation within 0.05 and
  all marginals within 0.01); ladder violation detection; period coupling and clamp rate;
  indicator correctness on handcrafted scenarios for every supported family and side;
  `max_marginal_error` below 0.01 on clean inputs; interval contains `p_joint` and lies within
  the Fréchet bounds; logical cases (over/under same line -> 0; -3.5 and -7.5 covers ->
  min) come out exact.

## Ownership

You own `rfqbot/pricing/*`, `tests/test_pricing_*.py`, `rfqbot/__main__.py`, and the NFL
matching files. `rfqbot/books.py`, `rfqbot/ladders.py` and book config belong to `totalis-6c`;
`rfqbot/sports/nba_*` belong to `totalis-c8`. Not a git repo: do not edit files you do not own.

## Report back

Marginal-consistency numbers on a real game once ladders exist (or synthetic until then), the
recovered margin-total correlation for a few games, the clamp rate, flags seen, a sample
pairwise table, timing for build and for a pair evaluation, and anything in this brief that
turned out wrong.
