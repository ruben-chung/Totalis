# Kalshi vs Polymarket venue weight, measured (2026-10-21)

Requested by the planning session (first item of `claude-sessions/future-work.md`): replace the 2.0 Kalshi
prior in the marginal layer (`MarginalConfig.kalshi_prior`, nfl and nba) with a measured number.
Script `rfqbot/pricing/venue_weight.py` (`python -m rfqbot.pricing.venue_weight [--json]`),
tests `tests/test_pricing_venue_weight.py`. Read-only over `data/books.db` and
`data/kalshi_books.db`. `rfqbot/marginals` is untouched (props session).

## Method

* Legs: `leg_matches` with `confidence = exact` (3,700 NFL, 760 NBA); Polymarket token via
  `leg_tokens`, Kalshi mid oriented to the leg's side (1 - yes mid for `no` matches).
* Window: the stores' overlap, 2026-10-20 22:35Z to 02:59Z (4.41 h), pre-game only (seconds
  before `game_start_time`; legs of games already in play are dropped).
* Grid: one second; mids carried forward from the last two-sided quote; a second is dropped when
  either feed's heartbeat (newest row store-wide, the marginal layer's confirmation rule) is older
  than 600 s (`stale_warn_pregame_s`), or either side has no two-sided quote yet. Feeds were alive
  on 100% of seconds (max gap: Polymarket 29 s, Kalshi 332 s).
* Lead-lag: for h in 1, 5, 15, 60 s, OLS of the next Polymarket mid change on the current
  Kalshi - Polymarket gap, and of the next Kalshi change on the Polymarket - Kalshi gap; the
  coefficient is the share of the gap that venue closes within h. The gap change on the gap gives
  the convergence half-life. Also with the gap demeaned per leg ("dm"), because exact matches
  still carry persistent offsets (rules, vig) that never converge. Game-clustered sandwich SEs.
* Noise: variance of the 1 s and 60 s mid changes per venue, pooled over games, ratio
  var_poly / var_kalshi with a 400-draw game-cluster bootstrap interval; once over all seconds and
  once over "tight" seconds where both half-spreads are at or under 0.02. The tight ratio is the
  one the prior should use: the marginal layer's sigma already charges the half-spread, so the
  prior multiplier must capture only the noise *beyond* the quoted spread.

## Results

Lead-lag, leg-demeaned gap (share of the gap closed within h; ± game-clustered SE):

| sport | h | Polymarket moves toward Kalshi | Kalshi moves toward Polymarket | half-life |
|---|---|---|---|---|
| NBA (668 legs, 36 games, 8.6M leg-s) | 1 s | +0.002 ± 0.000 | +0.000 ± 0.000 | 394 s |
| | 15 s | +0.013 ± 0.001 | +0.001 ± 0.000 | 743 s |
| | 60 s | +0.042 ± 0.004 | +0.002 ± 0.001 | 927 s |
| NFL (3,045 legs, 17 games, 48.2M leg-s) | 1 s | +0.002 ± 0.000 | +0.000 ± 0.000 | 324 s |
| | 15 s | +0.019 ± 0.001 | +0.003 ± 0.000 | 463 s |
| | 60 s | +0.055 ± 0.003 | +0.007 ± 0.001 | 652 s |

Kalshi leads in both sports: Polymarket closes 4-6% of a deviation per minute, Kalshi 0.2-0.7%.
Deviations halve in 5-15 minutes (faster at first, so not a single exponential). With the raw
gap the half-lives are 2-4x longer, which is the persistent offset, not slow convergence.

Noise (sd of mid changes; ratio var_poly / var_kalshi with 95% bootstrap interval):

| sport | seconds | h | sd Polymarket | sd Kalshi | ratio [95%] |
|---|---|---|---|---|---|
| NBA, all | 8.6M | 1 s | 0.00103 | 0.00044 | 5.5 [2.4, 9.2] |
| NBA, all | 8.6M | 60 s | 0.00471 | 0.00150 | 9.8 [3.2, 18.6] |
| NBA, tight (32 games) | 5.7M | 1 s | 0.00016 | 0.00022 | 0.51 [0.33, 1.02] |
| NBA, tight | 5.6M | 60 s | 0.00073 | 0.00078 | 0.90 [0.72, 1.13] |
| NFL, all | 48.2M | 1 s | 0.00414 | 0.00167 | 6.2 [5.3, 7.6] |
| NFL, all | 48.0M | 60 s | 0.02252 | 0.00799 | 8.0 [6.7, 9.9] |
| NFL, tight (15 games) | 16.9M | 1 s | 0.00021 | 0.00011 | 3.8 [2.2, 6.3] |
| NFL, tight | 16.6M | 60 s | 0.00084 | 0.00056 | 2.2 [1.5, 3.7] |

Half-spreads (per-leg median): NBA Polymarket p50 0.015 vs Kalshi 0.010; NFL Polymarket p50
0.050 (p75 0.175: many placeholder-wide books) vs Kalshi 0.015.

Sanity table against the current prior:

| sport | current | lead-lag says | tight 60 s ratio [95%] | all-seconds 60 s ratio | legs / games / seconds behind it |
|---|---|---|---|---|---|
| NBA | 2.0 | Kalshi leads (0.042 vs 0.002 per min) | 0.90 [0.72, 1.13] | 9.8 | 668 / 36 (32 tight) / 5.6M tight |
| NFL | 2.0 | Kalshi leads (0.055 vs 0.007 per min) | 2.2 [1.5, 3.7] | 8.0 | 3,045 / 17 (15 tight) / 16.6M tight |

## Recommendation

* **NFL: keep 2.0** (interval 1.5-3.7 at 60 s, 2.2-6.3 at 1 s). A value of 2.5 would sit in the
  middle of both; the current prior is inside the interval, so no change is warranted by 4 hours.
* **NBA: 1.0, not 2.0** (interval 0.72-1.13 at 60 s, 0.33-1.02 at 1 s). When both books are
  tight the Polymarket mid is at least as steady as Kalshi's; the 2.0 double-counts the spread,
  which the marginal layer already charges. The interval excludes 2.0.
* The lead-lag favouring Kalshi is about *who moves first*, not noise: Polymarket adjusts toward
  Kalshi, so a Kalshi quote carries earlier information. That argues for a staleness or
  lead term, not a precision multiplier; at the marginal layer's horizon (quotes refreshed every
  20 s, convergence half-life 5-15 min) it is worth at most a small tilt toward Kalshi, inside the
  NBA interval's upper end (1.1).

## Is 4 hours enough?

For the tight-quote ratios yes: the NFL 60 s interval is 1.5-3.7 over 15 games and the NBA one
0.72-1.13 over 32 games, both narrow enough to decide against or for 2.0. What the window cannot
tell: in-play behaviour (all in-play seconds were excluded), the last hour before kickoff (most
NFL games here are 3-6 days out, the NBA ones 1-3 days), and whether Kalshi's lead widens when
news hits. The all-seconds ratios (6-10) are dominated by wide Polymarket books and should not be
used as a prior multiplier.

## Not done / v2

* Repeat on a window that includes the hour before kickoff and in-play (the staleness gates
  differ there) before changing the in-play weighting.
* The lead-lag regression pools all lines of a game; a per-family split (moneyline vs ladders)
  would show whether the lead comes from the main lines only.
