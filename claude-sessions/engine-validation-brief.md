# Brief: validate the NFL joint engine on resolved games

Written 2026-10-20 by the planning session (`totalis-e0`). Owner: Ruben. For the NFL session.
Send a summary to `totalis-e0` when done.

## Why

The spec asks us to explain how correlations are estimated. The honest answer is a measured
calibration: on resolved games, does the engine's joint probability for a pair of legs match
the realized joint outcome better than independence does, and where does it not?

## Inputs (Data Engineer, `rfqbot/history`, done; price snapshots finishing)

- `history.db`: `hist_games`, `hist_markets`, `hist_legs` (with `won`), `hist_prices`
  (offsets 60/15/5 min before kickoff), `hist_pairs` (5.3M NFL pairs over 93 games, capped at
  60k per game with FG/1H/2H and moneyline/spread/total/team_total first).
- `rfqbot.history.export.game_markets_hist(game_prefix, offset_min, *, history_db=...)` returns
  `MarketQuote` with `best_bid = best_ask = p`, sizes None; legs without a snapshot come back
  unpriced (`include_unpriced=False` drops them). Check `python -m rfqbot.history stats` for
  snapshot coverage before starting; wait until the NFL prices job is complete.

## Deliverables (`rfqbot/pricing/validate.py` + CLI `python -m rfqbot joint-validate`)

1. For every resolved NFL game with snapshots at an offset (default 15 min): `build_game` on
   the historical quotes; record build success, ladders fitted, IPF residuals, flags,
   `max_marginal_error` against the snapshot prices, and time. Games the engine cannot build
   are reported with the reason, not skipped silently.
2. For a stratified sample of `hist_pairs` per game (all pairs if under the cap): engine
   `p_joint`, independent product `p_a * p_b`, and the realized `won_a * won_b`. Store in
   `hist_pair_scores(game, leg_a, leg_b, family_pair, p_joint, p_indep, lo, hi, won)`.
3. Report, by family pair (e.g. spread x total, spread x team_total, 1H spread x FG spread,
   moneyline x spread) and by lift bucket (`p_joint / p_indep` in <0.8, 0.8-1.2, 1.2-2, >2):
   mean realized minus mean predicted for the engine and for independence; Brier and log-loss
   for both; the share of realized outcomes inside `[lo, hi]` in a calibration sense (bucket
   by predicted, compare realized rate); counts. Also the single-leg calibration of the
   snapshot prices themselves (realized rate by predicted bucket) so engine error can be
   separated from market error.
4. The same at offsets 60 and 5 if cheap, to see whether closer snapshots help.
5. Write `claude-sessions/engine-validation-notes.md` with the tables and the three worst family pairs,
   with a guess at why. Keep the raw scores table so the backtest can reuse it.

## Ownership

You own `rfqbot/pricing/*` and `tests/test_pricing_*.py`; add a test that runs the scorer on
the recorded history fixture. Do not edit `rfqbot/history/*` (Data Engineer); ask it for any
export change. Not a git repo.
