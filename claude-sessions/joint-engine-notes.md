# NFL joint-distribution engine: what was built, what the data says (2026-10-20)

Built against `claude-sessions/joint-engine-brief.md`. Code: `rfqbot/pricing/` (`quotes.py`, `ladders_fit.py`,
`joint_nfl.py`, `indicators.py`, `joint.py`, `synthetic.py`, `cli.py`), command
`python -m rfqbot joint --game <slug> [--at-ms] [--pairs] [--synthetic]`, tests
`tests/test_pricing_*.py` (36 tests; whole suite 206 green).

## Construction (as implemented; deviations from the brief are marked *)

1. Quotes -> `LegSpec` via `nfl_poly.parse_poly_nfl`; fair = mid, err = half-spread + thin-book term
   (`quotes.fair_from_quote`, one function to replace later).
2. Ladders: legs oriented onto M = H - A, T, H, A per period; weighted isotonic fit (PAVA);
   observations > 3 err from the monotone fit flagged one at a time and down-weighted x0.01.
3. *IPF targets are **bins between quoted lines**, not a per-integer pmf. The brief's per-integer
   targets (from interpolated survival) cannot all be satisfied on the integer grid: the margin
   and total ladders then disagree on parity and on the interpolated splits, and the first IPF
   left L1 residuals of 0.02-0.06. With binned targets every quoted line is reproduced exactly
   (residuals 0 on real and synthetic games) and the prior fills inside bins and tails.
4. *Prior: when team-total ladders exist, a bivariate normal from the H/A ladders' own means and
   sds with Cov(H,A) = (Var T - Var M)/4; otherwise from the M/T ladders with `prior_rho_mt`.
   Tails outside the ladder use the ladder's own z-slope (clipped to 0.5-2x the default
   13.5 / 10.5), anchored at the outermost line.
5. Tie bucket: spreads start at 1.5, so {-1, 0, +1} is one bin; the moneyline (used as an
   observation at 0) splits it. P(M=0) = `tie_prob` 0.4% for the full game (overtime; engine
   choice), prior share for halves.
6. *Halves: the brief's Gaussian copula + clamp was replaced by a second IPF per component on
   the (X1, X) grid with structural zeros where X1 > X, fitted to the 1H and FG marginals
   (prior = bivariate normal with corr sd(X1)/sd(X)). Reason: with the copula even perfectly
   consistent synthetic halves clamped 9.4% of scenarios (4.5% per component, by construction
   of a Gaussian with these parameters), which biased 1H marginals by up to 0.05. With the IPF
   coupling both period marginals are exact, `clamp_rate` is 0, and the mass the IPF cannot
   place (`halves_residual`) is the disagreement measure. Scenarios: (H, A) from the FG table,
   H1 | H from the (H1, H) table, A1 | (A, H1) from the product of the (A1, A) conditional and
   the 1H table's A1 | H1 conditional.
7. Scenarios: 50,000 int16 (H1, A1, H, A). Indicators for moneyline (FG/1H/2H; 50-50 ties pay
   0.5), spread, total, team_total, btts, exact_margin; None for quarters, HSQ, safety, 2-pt,
   team TDs / yards.
8. Interval: min/max of p_joint under halves_corr +/-0.15, prior_rho -/+0.2, two bootstrap
   redraws; clipped to Fréchet. Shocked models are built lazily and cached.

## Numbers

Synthetic truth (bivariate normal, corr(H,A) = 0.3, sds 10/9, real ladder shapes, 264 quotes,
2% spreads): FG IPF residuals 0; corr(H,A) recovered 0.283 (truth 0.286), corr(M,T) 0.090
(0.113); every marginal within 0.002; max |engine - fair| over all 264 legs 0.0049 (1H legs
included); halves residual < 1e-7, clamp 0.

Real game, Patriots at Bears 2026-10-23 (the only NFL game inside the book recorder's 72 h
lookahead; 622 quotes, 596 two-sided): all four FG and all four 1H ladders fitted, IPF residuals
0. Over the 144 legs of fitted families with informative quotes (err <= 0.05):
max |engine - fair| 0.024, mean 0.003, none flagged. The 222 placeholder quotes (bid/ask like
0.01/0.99: exact margins, far ladder lines, props) differ by up to 0.48 and are reported
separately. Table corr(M,T) = +0.125, corr(H,A) = +0.168 (FG); 1H corr(H,A) = -0.04. p_tie FG
0.004 (engine), 1H 0.042 (market + prior share). Build 0.23 s (fit+IPF 0.14, scenarios 0.08);
first `joint()` call 0.55 s (builds the 6 shocked models), then 0.5 ms per pair.

Sample pairs (lift = joint / independent): FG CHI moneyline x FG CHI -1.5 cover 1.65 (0.608 vs
0.369 indep); 1H CHI team total under 10.5 x FG CHI team total under 23.5 2.39; 1H NE -1.5 cover
x FG NE -1.5 cover 2.22; totals vs spreads ~1.0 (corr(M,T) 0.12); same-market opposite sides
exactly 0; nested covers exactly min.

## Flags seen and what they mean

* `1H A sd is 1.10x the FG sd` (real game): the 1H spread/team-total ladders are *wider* than
  the full-game ones. Independent increments imply sd(1H)/sd(FG) ~ 0.7. Cause: far 1H lines
  (e.g. 1H spreads at 20.5, 21.5) are quoted around 5% instead of ~0.5%, with wide books, so
  the 1H tails are fat. The halves copula correlation then clips at 0.95. The marginals are
  still reproduced; the dependence between halves is what the flag questions. Candidate fix for
  v2: trust far lines only with narrow books, or cap the 1H sd at 0.8x FG when fitting the
  halves prior.
* `halves residual` stayed 0 on every game seen; it would be nonzero if a 1H ladder put more
  mass above a line than the FG ladder does.
* No ladder violations on the real game; the synthetic violation test (one quote shifted by
  0.25) flags only the culprit and leaves neighbours within 0.02.

## Things in the brief that turned out wrong or different

* "Team-total ladders are what pin the margin-total coupling": margin and total ladders alone
  already pin corr(H, A), because Var(T) - Var(M) = 4 Cov(H, A) (synthetic test without team
  totals recovers corr(H,A) 0.28 vs 0.286). What team totals add is the *margin-total*
  correlation (Var H - Var A = Cov(M, T)) and the split of the sds between the teams.
* Clamp rate < 2% is not achievable with a Gaussian copula between halves (see 6); replaced.
* Per-integer IPF targets are infeasible on the grid; binned targets (see 3).
* Polymarket lists no 0.5 spread line, so P(M = 0) and P(M = +/-1) are only jointly pinned;
  the moneyline splits them (see 5).
* The recorder's 72 h lookahead means only Thursday's game had books today; Sunday's slate
  needs `BOOK_GAME_LOOKAHEAD_HOURS=120` or waits until Thursday night.

## Not done / v2

Quarters; 2H ladders as a third fitted table (they exist on Polymarket and would make the
halves coupling data-driven instead of prior-driven); regulation-vs-OT split; rebuild debounce
(the engine is a pure function of the quote list; the caller decides when to rebuild).

## Update (2026-10-20, later): 2H table, calibration, placeholder quotes

* A 2H `PeriodModel` is built from the 2H ladders; when it passes a consistency gate (fitted,
  IPF residuals < 0.02, 1H + 2H means within 3 points of FG, 2H sd < FG sd) the halves coupling
  prior is independent increments with the 2H marginal instead of the Gaussian copula; otherwise
  the flags say so and the copula prior is used. Missing 1H/2H margin or total ladders are flags.
* `JointQuote.p_cal`: calibrated probability from `calibrate.py` (lambda and tail maps per family
  pair with game-clustered standard errors, precision-shrunk toward the group; market-pinned
  couplings stay at 1.0 unless the clustered interval excludes it; `calibration_level`,
  `coupling_kind`, `lambda_status`), fitted on resolved games
  (`joint-calibrate`, `rfqbot/pricing/calibration.json`); the interval is widened by the group's
  out-of-sample model error. Numbers in claude-sessions/engine-validation-notes.md, round 2.
* `fair_from_quote`: a sizeless quote at exactly 0.50 is a placeholder (err 0.25).
