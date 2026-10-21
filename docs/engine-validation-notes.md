# Joint engine validation on resolved NFL games (2026-10-20)

Built against `docs/engine-validation-brief.md`. Code `rfqbot/pricing/validate.py`, command
`python -m rfqbot joint-validate [--offsets 60,15,5] [--max-pairs 3000] [--report-only]`, scores in
`data/engine_validation.db` (`validation_games`, `hist_pair_scores`, `hist_leg_scores`; kept for the
backtest). Test: `tests/test_pricing_validate.py` (history fixture event, synthetic snapshots).

## Setup

93 resolved NFL games (2026-09-04 .. 2026-10-20) from `history.db`. Pre-kickoff snapshots exist
for the ~130 legs per game that traded (11,486 tokens over 93 games), so the engine builds from
partial ladders: bid = ask = snapshot price, every leg carries the thin-book error 0.02. Of the
5.35M `hist_pairs`, 95.5% have a leg without a snapshot and 1.9% a leg the engine does not model
(quarters, props, HSQ); the rest are sampled stratified by family pair, 3,000 per game ->
**141,819 pairs scored per offset**. Independence baseline = product of the two snapshot prices.

All 93 games built at every offset (mean 2.8 s incl. the six shocked models). Ladders fitted
(offset 15): all four FG ladders in 42 games, M+T plus one team total in 35, M+T only in 6, T only
in 3, nothing in 2 (prior only). 1H: margin in 86, totals in 29. 11 games have an FG IPF residual
above 0.02 (targets inconsistent, worst 0.26): the snapshot ladders contain 0.5 placeholders
(2,603 of 12,195 scored legs are priced exactly 0.5, i.e. untraded) which the isotonic fit flags
(FG/1H "ladder observation beyond 3 err" in ~40 games) and down-weights but cannot fully undo.
Engine-vs-snapshot error, excluding the 0.5 placeholders: mean 0.047, max 0.57 (offset 15); the
median per-game `max_marginal_error` including placeholders is 0.50 and is not informative.

Note on bias columns: `hist_pairs` holds all four side combinations of every market pair, so the
mean realized and both mean predictions are 0.25 by construction in every family-pair row. The
informative numbers are Brier / log-loss, the lift buckets and the calibration buckets.

## Headline (offset 15 min, 141,819 pairs)

| | engine | independence |
|---|---|---|
| Brier | **0.1303** | 0.1404 |
| log-loss | **0.4052** | 0.4323 |
| family pairs (n >= 200) where the engine is better | 22 of 29 | |

Offsets 60 and 5 give the same picture (Brier 0.1317 / 0.1312 vs 0.1410 / 0.1411): closer
snapshots do not help, which fits a model whose inputs are ladders of many lines rather than one
price. Single legs: the engine's own marginal (ladder fit) scores Brier 0.164 against 0.190 for the
raw snapshot prices, because the fit smooths out the 0.5 placeholders; market calibration of the
snapshots is otherwise fine (realized within ~0.02 of predicted in every bucket above 0.10).

## By lift bucket (p_joint / p_indep), offset 15

| lift | n | realized | engine | indep | Brier eng | Brier ind |
|---|---|---|---|---|---|---|
| < 0.8 | 47,977 | 0.081 | 0.064 | 0.170 | 0.0640 | 0.0786 |
| 0.8-1.2 | 51,699 | 0.383 | 0.384 | 0.381 | 0.1784 | 0.1789 |
| 1.2-2 | 31,578 | 0.328 | 0.342 | 0.220 | 0.1646 | 0.1844 |
| > 2 | 10,565 | 0.136 | 0.166 | 0.067 | 0.0939 | 0.1018 |

The direction of the dependence is right everywhere (where the engine says lift < 0.8 the pairs
pay 0.081 against the independent 0.170; where it says > 2 they pay 0.136 against 0.067) but the
magnitude is overstated by roughly a quarter: engine 0.064 vs realized 0.081 at the low end,
0.166 vs 0.136 at the high end.

## Calibration by predicted bucket (engine, offset 15)

| predicted | n | pred | realized | interval lo-hi | inside |
|---|---|---|---|---|---|
| < 0.05 | 39,587 | 0.015 | 0.023 | 0.010-0.022 | no |
| 0.05-0.10 | 18,037 | 0.073 | 0.080 | 0.060-0.086 | yes |
| 0.10-0.20 | 21,120 | 0.147 | 0.163 | 0.129-0.164 | yes |
| 0.20-0.50 | 36,476 | ~0.35 | ~0.34 | | yes |
| 0.50-0.60 | 8,165 | 0.549 | 0.547 | 0.528-0.572 | yes |
| 0.60-0.70 | 6,843 | 0.647 | 0.623 | 0.624-0.672 | no |
| 0.70-0.80 | 5,685 | 0.748 | 0.716 | 0.724-0.770 | no |
| 0.80-0.90 | 4,379 | 0.847 | 0.804 | 0.827-0.865 | no |
| > 0.90 | 1,527 | 0.929 | 0.898 | 0.909-0.942 | no |

Well calibrated between 0.05 and 0.60; overconfident in the tails (predicted 0.85 pays 0.80,
predicted 0.015 pays 0.023). Independence is calibrated across the range but with less
resolution (it never predicts the tails the engine reaches). The shock interval is too narrow to
cover the tail miscalibration: it measures input uncertainty, not model error.

## By family pair (n >= 2,000, offset 15; Brier engine / independence)

| family pair | n | Brier eng | Brier ind |
|---|---|---|---|
| spread/2H x spread/FG | 18,099 | 0.1265 | 0.1540 |
| spread/FG x total/FG | 17,573 | 0.1343 | 0.1349 |
| spread/1H x spread/FG | 16,646 | 0.1170 | 0.1268 |
| spread/FG x team_total/FG | 11,796 | 0.1344 | 0.1404 |
| spread/FG x spread/FG | 9,208 | 0.1219 | 0.1289 |
| spread/FG x team_total/2H | 9,137 | 0.1527 | 0.1623 |
| spread/FG x team_total/1H | 8,992 | 0.1406 | 0.1401 |
| spread/2H x total/FG | 6,820 | 0.1256 | 0.1501 |
| spread/1H x total/FG | 6,751 | 0.1120 | 0.1209 |
| total/FG x total/FG | 5,196 | 0.1199 | 0.1301 |
| exact_margin/FG x spread/FG | 4,989 | 0.1066 | 0.1252 |
| team_total/FG x total/FG | 4,591 | 0.1287 | 0.1355 |
| team_total/1H x total/FG | 3,529 | 0.1322 | 0.1343 |
| spread/FG x total/2H | 3,344 | 0.1500 | 0.1570 |
| team_total/2H x total/FG | 3,281 | 0.1337 | 0.1491 |
| spread/FG x total/1H | 2,978 | 0.1603 | 0.1563 |

The biggest wins are exactly where independence is most wrong: half spreads against full-game
spreads (a 2H spread and the FG spread share the second-half score), nested same-family pairs
(two spreads, two totals), and exact margins against spreads (sub-events of the same margin).
Spread x total, the classic same-game parlay, is a wash: the FG margin-total correlation is
small (+0.1 to +0.15 in the fitted tables) and the snapshot prices already carry it.

## The three worst family pairs (n >= 200, engine Brier minus independence Brier)

1. **moneyline/2H x total/FG** (n = 220, +0.0155 at 15 min; also worst at 60 and 5). The 2H
   moneyline is modeled as "FG minus 1H" with a 50-50 tie; only 220 pairs, from the handful of
   games whose 2H moneyline traded. Guess: the 1H/FG coupling prior (independent increments,
   Gaussian shape inside the IPF) misplaces the 2H margin's tie and near-tie mass, which is
   exactly what a 2H moneyline pays on; the snapshot for the 2H moneyline is also one of the
   thinnest markets (often the 0.5 placeholder).
2. **moneyline/1H x spread/FG** (n = 498, +0.0057; +0.0202 at 5 min). Same mechanism on the
   first half: the 1H margin pmf comes from the 1H spread ladder, which in the history has far
   lines at placeholder prices (the "1H sd > FG sd" flag fired in ~40 games), so the 1H winner's
   tie bucket (prior share, ~4%) and its coupling to the FG margin are the least pinned parts of
   the model.
3. **spread/FG x total/1H** (n = 2,978, +0.0039) and **spread/FG x team_total/1H** (n = 8,992,
   +0.0005): 1H totals are fitted in only 29 of 93 games (5 lines when present), so the 1H total
   is mostly the prior scaled by 1/sqrt(2) and coupled to the FG table through the halves IPF;
   independence, using the actual 1H snapshot, knows the 1H total level better than the prior.

Common thread: everything that depends on the first-half tables and the halves coupling is
weaker than the full-game machinery, and the engine's tails are too tight. Two cheap fixes to
try next: (a) fit the 2H ladders as a third table (Polymarket lists 2H spreads/totals/team totals
for every game) so the halves coupling is data-driven; (b) a tail-shrink calibration (one
parameter, fit on this table) applied to p_joint, which the backtest can own.

## Reading the raw table

`hist_pair_scores(game_prefix, offset_min, leg_a, leg_b, family_pair, p_joint, p_indep, p_leg_a,
p_leg_b, mkt_a, mkt_b, lo, hi, won, won_a, won_b)` with canonical LegSpec keys as leg ids;
`hist_leg_scores(game_prefix, offset_min, leg, family, period, mkt, p_engine, won)`. Both are
replaced per (game, offset) on a re-run; the pair sample is stratified by family pair with a
floor per family and a fixed seed.

## Round 2 (2026-10-20, later): 2H ladders as a third table, and a calibration layer

**2H table.** The engine now fits a 2H `PeriodModel` from the 2H ladders and, when it is
consistent, uses it as the halves-coupling prior: `prior[x1, x] = p1(x1) p2(x - x1)` (independent
increments with the fitted 2H marginal) instead of the Gaussian copula. "Consistent" means at
least one 2H ladder fitted, 2H IPF residuals under 0.02, 1H + 2H means within 3 points of the FG
means and 2H dispersion below the FG one; otherwise the flags say
`halves coupling: <reason>; Gaussian prior with corr sd(1H)/sd(FG)` and missing half ladders are
flagged (`1H T: 0 half-point line(s), using FG-scaled prior`, 64 games; `2H T` 59 games).
On the resolved-game snapshots the gate accepts the 2H table in **9 of 93 games**; the other 84
have 2H ladders that are placeholders or disagree with 1H + FG (e.g. "1H + 2H mean A = 25.7 vs
FG 17.7"). On the live NE@CHI books the 2H table also fails the gate (2H IPF residual 0.58).
So the fix is in place and tested (synthetic 2H ladders drive the coupling; a garbage 2H ladder
is rejected), but the data rarely lets it act. Re-run at offset 15 (`data/engine_validation_v2.db`):
Brier 0.1302 vs 0.1303, 22 of 29 family pairs better than independence, same lift and tail
picture. Three worst family pairs again: moneyline/2H x total/FG (+0.0137, n 220),
moneyline/1H x spread/FG (+0.0067, n 498), moneyline/FG x team_total/2H (+0.0049, n 216);
spread/FG x total/1H is fourth (+0.0039, n 2,978). Unchanged, as expected when the coupling
prior almost never changes.

**Calibration layer** (`rfqbot/pricing/calibrate.py`, `python -m rfqbot joint-calibrate`,
parameters in `rfqbot/pricing/calibration.json`, loaded by the engine; `JointQuote.p_cal`,
`group`, `model_error`, `calibrated`; quote assembly prices on `p_cal`):
`p_cal = p_indep + lambda_g (p_joint - p_indep)` with `p_indep` the product of the engine's own
leg marginals, lambda per group (within_FG, within_half, cross), then affine tail maps below 0.05
and above 0.80; logical combos (impossible, nested) stay exact. Fitted on the v2 scores with
5-fold cross-validation by game (91 games with engine marginals, 141,819 pairs):

| group | n | lambda | model error (OOS) | Brier engine | Brier indep | Brier cal (OOS) | log-loss engine | log-loss cal (OOS) |
|---|---|---|---|---|---|---|---|---|
| within_FG | 56,701 | 1.002 | 0.012 | 0.1274 | 0.1310 | 0.1275 | 0.3918 | 0.3925 |
| within_half | 84 | 0.923 | 0.000 | 0.1907 | 0.2040 | 0.1904 | 0.5729 | 0.5704 |
| cross | 85,034 | 0.804 | 0.012 | 0.1320 | 0.1335 | 0.1320 | 0.4144 | 0.4106 |
| all | 141,819 | | | 0.1302 | 0.1325 | 0.1302 | 0.4055 | 0.4035 |

Tails (out of sample): predicted < 0.05, engine 0.015 -> realized 0.023, calibrated 0.023;
predicted > 0.80, engine 0.868 -> realized 0.829, calibrated 0.831. Bucket residual (the model
error the interval now carries) 0.0147 -> 0.0102.

Reading: the full-game dependence is right as is (lambda 1.00); the cross-period coupling is
overstated by a fifth (lambda 0.80), which is where the three worst family pairs live; the
tails were overconfident and are now calibrated. The Brier gain is small because the
miscalibration is concentrated in the tails (few pairs, small absolute errors) and the
cross-period shrink moves probabilities by ~0.01; log-loss, which weights the tails, improves
0.4055 -> 0.4035. "indep" in this table is the product of engine marginals (0.1325), better than
the snapshot product used in the first round (0.1404) because the ladder fit removes the 0.5
placeholders; `fair_from_quote` now gives a sizeless quote at exactly 0.50 an error of 0.25 so
the live engine treats such legs the same way.

The interval `[lo, hi]` is now the shock range shifted by the calibration and widened by the
group's out-of-sample model error (0.012 for FG and cross pairs), clipped to Fréchet; quote
assembly's `u_d` (half the width) therefore covers model error, not only input uncertainty.

## Round 3 (2026-10-20, later): calibration measured per market-type pair

Change request: measure lambda and the tail maps per family pair (family/period x family/period
of the two legs), not pooled into three groups, fitted hierarchically because tails are thin:
`value_used = w * value_pair + (1 - w) * value_group`, `w = n / (n + n0)` with `n` the pair's
rows (tail rows for the tail maps) and `n0` chosen by 5-fold cross-validation by game on
out-of-sample log-loss. `rfqbot/pricing/calibration.json` is keyed by family pair with the group
fallbacks included; `JointQuote.calibration_level` is `pair`, `group` (pair unseen in the fit)
or `none` (no file / single leg / logical combo); model error for the interval is per pair with
group fallback. Fit on the v2 scores (91 games, 141,819 pairs):

| | engine | indep (engine marginals) | pooled-group cal (OOS) | per-pair cal (OOS) |
|---|---|---|---|---|
| Brier | 0.1302 | 0.1325 | 0.1302 | 0.1302 |
| log-loss | 0.4055 | 0.4155 | 0.4035 | **0.4026** |
| bucket residual (model error) | 0.0147 | | | 0.0099 |

Chosen `n0` = 10,000 for lambda (only pairs with thousands of rows keep much of their own value)
and 300 tail rows for the tail maps. Group lambdas: within_FG 1.00, cross 0.80, within_half 0.92
(n 84). Per family pair (n >= 1,000; `lam_pair` is the pair's own estimate, `lam_used` the shrunk
value; tails are `a + b p` below 0.05 / above 0.80; Brier and log-loss out of sample):

| family pair | n | n_lo | n_hi | lam_pair | lam_grp | lam_used | lo a,b | hi a,b | resid | Brier eng | Brier pair | ll eng | ll pair |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| spread/2H x spread/FG | 18,099 | 6,039 | 1,138 | 1.052 | 0.804 | 0.964 | +0.008,1.412 | -0.482,1.485 | 0.022 | 0.1260 | 0.1258 | 0.3967 | 0.3926 |
| spread/FG x total/FG | 17,573 | 4,098 | 556 | 0.278 | 1.002 | 0.541 | -0.005,1.197 | -0.029,1.002 | 0.012 | 0.1343 | 0.1343 | 0.4167 | 0.4160 |
| spread/1H x spread/FG | 16,646 | 4,955 | 872 | 0.676 | 0.804 | 0.724 | -0.005,0.958 | -0.275,1.312 | 0.013 | 0.1170 | 0.1169 | 0.3688 | 0.3655 |
| spread/FG x team_total/FG | 11,796 | 2,979 | 334 | 1.022 | 1.002 | 1.013 | +0.005,0.987 | -0.284,1.295 | 0.021 | 0.1340 | 0.1343 | 0.4118 | 0.4126 |
| spread/FG x spread/FG | 9,208 | 2,992 | 512 | 0.940 | 1.002 | 0.972 | -0.002,1.013 | -0.557,1.602 | 0.034 | 0.1217 | 0.1219 | 0.3679 | 0.3687 |
| spread/FG x team_total/2H | 9,137 | 2,504 | 380 | 1.142 | 0.804 | 0.965 | +0.046,0.736 | +0.170,0.661 | 0.047 | 0.1526 | 0.1519 | 0.4928 | 0.4744 |
| spread/FG x team_total/1H | 8,992 | 1,928 | 191 | 0.573 | 0.804 | 0.694 | +0.013,0.621 | +0.534,0.334 | 0.018 | 0.1410 | 0.1407 | 0.4415 | 0.4357 |
| spread/2H x total/FG | 6,820 | 2,223 | 402 | 0.613 | 0.804 | 0.727 | +0.011,1.131 | -0.203,1.183 | 0.023 | 0.1255 | 0.1258 | 0.3969 | 0.3940 |
| spread/1H x total/FG | 6,751 | 1,825 | 315 | 0.616 | 0.804 | 0.728 | +0.004,0.533 | +0.333,0.641 | 0.030 | 0.1124 | 0.1125 | 0.3560 | 0.3563 |
| total/FG x total/FG | 5,196 | 1,583 | 206 | 1.250 | 1.002 | 1.087 | +0.001,0.181 | -0.355,1.382 | 0.018 | 0.1200 | 0.1201 | 0.3615 | 0.3608 |
| exact_margin/FG x spread/FG | 4,989 | 1,799 | 393 | 0.989 | 1.002 | 0.998 | -0.002,0.964 | +0.115,0.841 | 0.026 | 0.1065 | 0.1066 | 0.3311 | 0.3318 |
| team_total/FG x total/FG | 4,591 | 1,212 | 149 | 1.214 | 1.002 | 1.069 | +0.008,0.936 | +0.340,0.574 | 0.015 | 0.1284 | 0.1286 | 0.3992 | 0.4007 |
| team_total/1H x total/FG | 3,529 | 783 | 75 | 1.149 | 0.804 | 0.894 | +0.007,0.305 | +0.033,0.910 | 0.025 | 0.1326 | 0.1333 | 0.4065 | 0.4095 |
| spread/FG x total/2H | 3,344 | 645 | 37 | 0.000 | 0.804 | 0.602 | +0.001,0.901 | +0.167,0.763 | 0.027 | 0.1498 | 0.1490 | 0.4554 | 0.4527 |
| team_total/2H x total/FG | 3,281 | 949 | 121 | 1.125 | 0.804 | 0.883 | +0.031,0.492 | +0.316,0.546 | 0.049 | 0.1338 | 0.1338 | 0.4326 | 0.4196 |
| spread/FG x total/1H | 2,978 | 474 | 10 | 0.000 | 0.804 | 0.619 | -0.002,1.390 | -0.021,0.975 | 0.054 | 0.1602 | 0.1597 | 0.4859 | 0.4829 |
| exact_margin/FG x total/FG | 1,916 | 695 | 160 | 0.495 | 1.002 | 0.920 | -0.004,0.773 | -0.139,1.151 | 0.034 | 0.1042 | 0.1043 | 0.3264 | 0.3259 |
| total/2H x total/FG | 1,302 | 331 | 18 | 1.205 | 0.804 | 0.850 | -0.002,1.171 | -0.021,0.975 | 0.036 | 0.1322 | 0.1327 | 0.3992 | 0.4027 |
| total/1H x total/FG | 1,210 | 284 | 14 | 1.060 | 0.804 | 0.831 | +0.006,0.423 | -0.021,0.975 | 0.061 | 0.1443 | 0.1448 | 0.4229 | 0.4260 |

The full table (41 pairs, including the moneyline pairs with 60-520 rows, which all shrink to
their group) is printed by `python -m rfqbot joint-calibrate --scores-db data/engine_validation_v2.db --dry-run`.

What the per-pair view shows that the groups hid:

* **spread/FG x total/FG** (the classic same-game parlay) has `lam_pair` 0.28: the engine's
  full-game margin-total coupling is largely not realized. The pooled within_FG lambda of 1.00
  was an average of this pair with the nested spread x spread (0.94), spread x team total
  (1.02), total x total (1.25) and team total x total (1.21) pairs, where the dependence is
  structural and fully realized. With `n0` = 10,000 the used value is 0.54.
* **spread/2H x spread/FG** (1.05) and **spread/FG x team_total/2H** (1.14) are fully realized:
  the second-half coupling is right. The cross-group lambda of 0.80 came from the
  first-half pairs: spread/1H x spread/FG 0.68, spread/FG x team_total/1H 0.57,
  spread/1H x total/FG 0.62, and the two pairs with lam_pair 0 (spread/FG x total/1H,
  spread/FG x total/2H: the engine's half-total vs full-spread coupling carries nothing).
* Tail maps differ by pair as well: the hi-tail slope is above 1 for the nested spread pairs
  (the engine under-predicts the very likely nested covers) and below 1 for the team-total pairs.
* Moneyline pairs (the round-1 worst) have 60-520 rows each and shrink almost entirely to the
  group; they remain the pairs with the largest residual model error (0.04-0.09), so their
  interval is widened the most.

The out-of-sample gain over the pooled fit is small in Brier (unchanged at 4 decimals) and
visible in log-loss (0.4035 -> 0.4026) and bucket residual (0.0102 -> 0.0099): the information is
in a handful of large pairs, and the shrinkage protects the rest.

## Round 4 (2026-10-20, later): game-clustered evidence and market-pinned couplings

Change request: rows within a game share the game's outcome, so the effective sample for any
coupling question is the number of games (93), and row-count shrinkage (round 3, n0 = 10,000
rows) trusted a pair like spread/FG x total/FG about a hundred times more than its evidence
supports. New rules in `calibrate.py`:

1. Every pair's lambda and tail-map parameters carry a **game-clustered** standard error
   (sandwich estimator with game clusters); the report shows `lam_pair` with its 95% interval
   and the number of games behind it.
2. Shrinkage is by precision: `lambda_used = (lambda_pair / var_pair + lambda_group / var_group)
   / (1/var_pair + 1/var_group)` with clustered variances; same per tail-map parameter on the
   tail rows.
3. Pair classes: `market_pinned` (both legs full game on different score variables; the
   coupling is read from the four FG ladders) keep lambda = 1.0 unless the clustered 95%
   interval excludes 1.0 (`uninformative` vs `informative`, in which case rule 2 applies);
   `structural` (both legs full game on the same variable: two spreads, spread x moneyline,
   exact margin x spread, two totals) are fixed at 1.0; `prior` (any half leg; the coupling is the
   engine's halves prior) shrink freely under rule 2. `JointQuote` exposes `coupling_kind` and
   `lambda_status`; `calibration_level` (pair | group | none) is unchanged.

Result (v2 scores, 91 games, 141,819 pairs, 5-fold CV by game, out of sample): Brier engine
0.1302, pooled-group 0.1302, per-pair 0.1302; log-loss 0.4055 -> 0.4035 (pooled) -> 0.4031
(per-pair); bucket residual 0.0147 -> 0.0108. Status mix over the 67 pairs seen: 7 market-pinned,
all `uninformative`; 6 structural `fixed`; 23 prior `shrunk`; 31 prior pairs seen in one game
only, on the group value.

| family pair | kind | n | games | lam_pair [95% CI] | lam_group | lam_used | status |
|---|---|---|---|---|---|---|---|
| spread/2H x spread/FG | prior | 18,099 | 89 | 1.05 [+0.61, +1.49] | 0.80 | 0.84 | shrunk |
| spread/FG x total/FG | market_pinned | 17,573 | 90 | 0.28 [-0.72, +1.28] | 1.00 | **1.00** | uninformative |
| spread/1H x spread/FG | prior | 16,646 | 89 | 0.68 [+0.42, +0.93] | 0.80 | 0.76 | shrunk |
| spread/FG x team_total/FG | market_pinned | 11,796 | 89 | 1.02 [+0.78, +1.27] | 1.00 | 1.00 | uninformative |
| spread/FG x spread/FG | structural | 9,208 | 87 | 0.94 [+0.74, +1.14] | 1.00 | 1.00 | fixed |
| spread/FG x team_total/2H | prior | 9,137 | 85 | 1.14 [+0.44, +1.85] | 0.80 | 0.82 | shrunk |
| spread/FG x team_total/1H | prior | 8,992 | 87 | 0.57 [+0.19, +0.96] | 0.80 | 0.77 | shrunk |
| spread/2H x total/FG | prior | 6,820 | 80 | 0.61 [-0.48, +1.70] | 0.80 | 0.80 | shrunk |
| spread/1H x total/FG | prior | 6,751 | 80 | 0.62 [-0.79, +2.02] | 0.80 | 0.80 | shrunk |
| total/FG x total/FG | structural | 5,196 | 80 | 1.25 [+0.99, +1.51] | 1.00 | 1.00 | fixed |
| exact_margin/FG x spread/FG | structural | 4,989 | 78 | 0.99 [+0.51, +1.47] | 1.00 | 1.00 | fixed |
| team_total/FG x total/FG | market_pinned | 4,591 | 80 | 1.21 [+0.90, +1.53] | 1.00 | 1.00 | uninformative |
| team_total/1H x total/FG | prior | 3,529 | 78 | 1.15 [+0.49, +1.81] | 0.80 | 0.83 | shrunk |
| spread/FG x total/2H | prior | 3,344 | 68 | 0.00 [-1.02, +1.02] | 0.80 | 0.78 | shrunk |
| team_total/2H x total/FG | prior | 3,281 | 77 | 1.13 [+0.51, +1.74] | 0.80 | 0.83 | shrunk |
| spread/FG x total/1H | prior | 2,978 | 65 | 0.00 [-1.59, +1.59] | 0.80 | 0.80 | shrunk |
| exact_margin/FG x total/FG | market_pinned | 1,916 | 72 | 0.50 [-3.89, +4.88] | 1.00 | 1.00 | uninformative |
| moneyline/1H x spread/FG | prior | 498 | 15 | 0.44 [-0.45, +1.32] | 0.80 | 0.79 | shrunk |
| moneyline/2H x spread/FG | prior | 448 | 18 | 1.82 [+1.05, +2.59] | 0.80 | 0.85 | shrunk |

As expected: spread/FG x total/FG returns to 1.0, marked uninformative (its 0.28 came with a
clustered interval of [-0.72, +1.28]; 90 games cannot confirm or reject a correlation of ~0.12);
the first-half pairs keep a real shrink (0.68 -> 0.76, 0.57 -> 0.77, intervals that exclude 1.0);
nested pairs are unchanged at 1.0. The two "lambda 0" pairs of round 3 (half totals against the
full-game spread) have intervals of +/-1 or wider and sit on the group value. The remaining
moneyline pairs have 14-18 games and intervals a full unit wide; they stay close to the group
and keep the largest model error (0.04-0.09) in their interval.

Two things the clustered view makes explicit: with 93 games nothing finer than the group-level
first-half shrink (0.80 +/- 0.09) is really measured, and the group's own clustered variance is
an estimation variance, not a between-pair heterogeneity (a proper empirical-Bayes prior
variance); with it as the prior, a pair with a precise own estimate (spread/2H x spread/FG,
1.05 +/- 0.22) still lands near the group (0.84). Estimating the between-pair variance needs
more games than we have; noted as future work.
