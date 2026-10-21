# Margin optimiser: notes

Written 2026-10-21 by the NBA session (totalis-c8) for the planning session (totalis-e0) and Ruben.
Brief: `docs/margin-optimizer-brief.md`. Code: `rfqbot/edge/` (owner nba). Tests: `tests/test_edge_*.py` (32).
Run: `python -m rfqbot.edge fit [--since-hours N]`, `report`, `wallets [--allow|--deny|--clear ID]`,
`monitor` (fit every `EDGE_REFRESH_SEC`, 300 s). Output `rfqbot/edge/margin_table.json`, state `data/edge.db`.

## What runs

- Paper quoting listener (step 0), started 2026-10-20 21:21 local on its own database:
  `RFQ_DB_PATH=data/rfq_quote.db RAW_LOG_DIR=none QUOTE_DECISIONS_DB=data/decisions.db rfqbot listen --quote`.
  `scripts/run.sh` keys its duplicate check on `rfqbot listen`, which the recording listener already
  matches, so it was started by hand with the same `nohup caffeinate` wrapper and logs to
  `data/logs/rfqbot_listen_quote.log`. It writes ~1,500 quoted decisions per hour (240k decisions per hour
  in total; the rest are skips, mostly `sport_not_quoted`).
- `scripts/run.sh rfqbot.edge monitor --since-hours 48`: a full fit every 5 min (markouts are incremental, a
  fit costs ~5 s plus ~0.2 s per new (game, minute) fair lookup).

## Pipeline (one `fit`)

1. `fills.py`: each quoted decision joined to the trade print on its rfq_id (`data/rfq.db`, the recording
   listener). Win iff `m <= delta`, `delta = print - p_fair` (BUY) or `p_fair - print` (SELL); fill
   fraction `min(1, our size / printed size)`; decisions with no print never fill. The requester is not
   in `decisions.db`, so it is joined from `rfqs -> requesters` by rfq_id; the round-trip tag is read from
   the decision's reason text (`[round_trip]`).
2. `fillcurve.py`: F(m) on a 0..10 pt grid (0.25 pt steps) per node, PAVA-monotone, pooled up the ladder
   (drop sigma bucket, then structure, then n_legs, then sport, then direction = global) until the node has
   `EDGE_N_MIN` (30) decisions. Segment key `sport|n_legs|direction|structure|sigma`, sigma buckets of
   u_m + u_d at `EDGE_SIGMA_CUTS` 0.005 / 0.009 (the live distribution clusters at 0.3, 0.4, 0.6 and 1.0 pt).
3. `curse.py`: model `E[loss | win] = sigma * phi(z)/Phi(z)`, `z = (delta - m)/sigma`, delta = mean
   competitor gap of the node's printed decisions (unconditionally `E[print - p_fair] = delta`); measured =
   mean of `later fair - p_fair` (maker loss sign) among decisions that would have won at m, later fair =
   `marginals.compute_game` at print + 15 / 30 min on the model, Polymarket-only and Kalshi-only references.
4. `markout.py`: every print in the quoted sports, maker P&L of the fair move at +1/5/15/30 min on the three
   references, segment and requester attached; rows are final once stored (the books never backfill), only
   immature horizons are revisited.
5. `wallets.py`: per requester, profit per fill = markup + 30-min model markout, shrunk toward the population
   with 8 pseudo-fills; block when n >= 8 and the one-sided `EDGE_BLOCK_CONF` upper bound < 0, 7-day expiry
   unless re-triggered, round trip = immediate block, manual allow/deny in `data/edge_wallets.json`,
   premium `EDGE_WALLET_K * max(0, -shrunk)` below the threshold.
6. `optimize.py`: `m* = argmax F(m) (m - curse(m))` s.t. the fill-rate bound; `MarginTerms` with
   `curse_mult = phi/Phi at z*` and `base = m* - curse_mult * sigma_node`, so the quote-time rule
   `margin = base + curse_mult * (u_m + u_d) + info_premium + wallet_premium` gives exactly m* at the node's
   sigma and charges the extra expected curse for a wider RFQ. `info_premium = max(0, -mean 30-min model
   markout)` pooled the same way (first node on the ladder with 30 markouts).
7. `table.py`: `margin_for(segment, requester=) -> MarginTerms(base, curse_mult, info_premium,
   wallet_premium, blocked, segment, level, n, source, block_reason)`; nodes below `n_min` are not written,
   the lookup walks the ladder to the deepest node present; without a table it returns the default edge.
   Signature sent to the NFL session 2026-10-21.

## First tables (fit of 2026-10-21 01:35 UTC, 8-hour window; refreshed below when more data exists)

Decisions: 3,513 quoted (22:42 -> 01:35 UTC: the NFL session's two 10-minute runs plus the continuous
listener from 01:21 UTC). Prints in the quoted sports: 196 (nba BUY 160, nba SELL 17, nfl BUY 16, mixed 2).
Only 40 of those prints have any decision row (the quoting listener covered ~50 min of the 4 h of
`rfq.db`): 17 skipped `unsupported:nba_same_game`, 15 quoted, 7 `size_below_min`, 1 `leg:inplay`.
**15 printed decisions, 5 would have won at our actual quote.** That is the whole counterfactual sample
tonight; every number below is a first reading, not an estimate.

Full cells (n, printed, wins, node used, pooling level):

| cell | n | printed | wins | node | lvl |
|---|---|---|---|---|---|
| nba\|2\|BUY\|disjoint\|s0 | 868 | 7 | 2 | itself | 0 |
| nba\|2\|BUY\|disjoint\|s1 | 976 | 2 | 2 | itself | 0 |
| nba\|2\|BUY\|disjoint\|s2 | 156 | 3 | 0 | itself | 0 |
| nba\|2\|SELL\|disjoint\|s0/s1/s2 | 256 / 686 / 112 | 0 | 0 | itself | 0 |
| nba\|3-4\|BUY\|disjoint\|s0 | 18 | 1 | 0 | nba\|*\|BUY\|*\|* | 3 |
| nfl\|2\|BUY\|disjoint\|s0 | 60 | 2 | 1 | itself | 0 |
| nfl\|2\|BUY\|same_game\|s1 / s2 | 110 / 151 | 0 | 0 | itself | 0 |
| mixed\|2\|BUY\|disjoint\|s0/s1 | 8 / 6 | 0 | 0 | mixed\|2\|BUY\|disjoint\|* | 1 |
| mixed\|2\|SELL\|disjoint\|s2, nfl\|2\|SELL\|disjoint\|s0 | 2 / 1 | 0 | 0 | *\|*\|SELL\|*\|* | 4 |

Fill curves and optimiser (nodes with prints; pt = probability points):

| node | n | printed | F(0) | F(1pt) | F(2pt) | delta | sigma | m* | F(m*) | curse* | info | c_mult | base |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| nba\|2\|BUY\|disjoint\|s0 | 868 | 7 | 0.8% | 0.2% | 0.1% | 0.91 | 0.33 | 0.50 | 0.6% | 0.07 | 0.13 | 0.20 | 0.43 |
| nba\|2\|BUY\|disjoint\|* | 2,000 | 12 | 0.6% | 0.3% | 0.2% | 2.79 | 0.67 | 2.50 | 0.2% | 0.37 | 0.00 | 0.54 | 2.13 |
| nba\|*\|BUY\|*\|* | 2,020 | 13 | 0.6% | 0.3% | 0.2% | 2.62 | 0.67 | 2.50 | 0.2% | 0.46 | 0.00 | 0.69 | 2.04 |
| *\|*\|BUY\|*\|* | 2,456 | 15 | 0.6% | 0.3% | 0.2% | 2.42 | 1.04 | 2.50 | 0.2% | 0.88 | 0.00 | 0.85 | 1.62 |
| global | 3,513 | 15 | 0.4% | 0.2% | 0.1% | 2.42 | 0.92 | 2.50 | 0.1% | 0.79 | 0.00 | 0.85 | 1.71 |

Every SELL node and the NFL nodes have decisions but no print (`source = no_prints`): the loader hands
back the default edge for them. F(m) is per request: 99% of quoted RFQs never trade, so F(0) is 0.4-0.8%
and the fill-rate bound `EDGE_MIN_FILL_RATE` is applied to F among printed RFQs by default
(`EDGE_MIN_FILL_BASIS=print`); `request` uses F itself.

Winner's curse, measured vs model (maker loss in pt, at m*): the model says 0.07 pt for the s0 cell
(sigma 0.33, delta 0.91) and 0.4-0.9 pt for the pooled nodes; the measured conditional move among the
would-have-won decisions is +1.6 pt at 15 min and -2.2 pt at 30 min on both the model and the Polymarket
reference (n = 2), +1.6 / +3.4 pt on Kalshi (n = 1), and +1.6 pt at 30 min (n = 3) in the s0 cell. With 2-3
winners per node the sign flips between horizons; the comparison is wired, not yet informative.

Markouts (maker P&L in pt after the print, model reference; Polymarket within 0.1 pt of it):

| horizon | BUY n | BUY mean (se) | SELL n | SELL mean (se) |
|---|---|---|---|---|
| 1 min | 160 | +0.04 (0.10) | 14 | -0.34 (0.17) |
| 5 min | 160 | +0.39 (0.19) | 14 | -0.41 (0.19) |
| 15 min | 152 | +0.42 (0.43) | 14 | -0.57 (0.59) |
| 30 min | 143 | +0.79 (0.38) | 14 | -0.18 (0.41) |

Kalshi reference (only combos whose legs are all matched): BUY +0.2..+0.7 pt (n 42-51), SELL -0.8..-1.4 pt
(n = 3). By sport over all horizons: nba BUY +0.43 (n 566), nba SELL -0.37 (n 56), nfl BUY +0.02 (n 49).
So BUY flow pays the maker on average and SELL flow costs 0.3-0.6 pt, the direction the brief measured
(sell -0.25 at 15 min, -0.57 at 30 min); tonight's SELL sample is 14 prints. The informational premium is
therefore zero on every BUY node and would be ~0.2-0.6 pt on SELL nodes once they have 30 markouts.
31% of (print, reference, horizon) rows have no fair at the print time: prints during a game, where the
marginal layer's in-play rule returns no fair. Those rows are final.

Wallets: 112 requesters scored on 157 observations, **0 blocked, 0 with a premium**. Nobody is below
8 fills with a negative upper bound; the most negative shrunk scores are +1.7..+3 pt because the markup
credited to a print without a decision is the node's m* (2.5 pt) and BUY markouts are positive.

## What did not hold

1. **The sequential block rule's false-positive rate is ~22% at 90%, and that is accepted.** The rule
   is evaluated at every refresh, i.e. after every fill from the 8th, so it is a sequential test with
   ~30 looks. Simulated neutral wallets (profit ~ N(0, 2 pt), 40 fills, population of 20 such wallets):

   | EDGE_BLOCK_CONF | 0.90 | 0.95 | 0.975 | 0.99 |
   |---|---|---|---|---|
   | false-positive rate | 22.5% | 3.5% | 4.5% | 1.0% |

   Ruben's decision (2026-10-21): keep 0.90. The payoff is skewed: blocking a wallet that is not sharp
   costs a little flow for 7 days, failing to block a sharp one costs EV on every fill. What matters is
   detection: a -2 pt wallet (sd 2 pt) is caught in 97%+ of runs with a median of <= 16 fills at 0.90.
   `tests/test_edge_wallets.py` pins both the false-positive band and the detection speed.
2. **Wallet profit per fill is scored on prints, not fills.** Paper mode has no fills and 5 counterfactual
   wins tonight, so a wallet's observations are its prints in the quoted sports, each credited with our
   logged markup when we quoted that RFQ and the node's m* otherwise, plus the 30-min model markout.
   `n_wins` on the score records how many were real counterfactual wins.
3. **Counterfactual sample is thin** (15 printed decisions in 8 h): the quoting listener was only up for
   ~50 min of the window, 17 of the 40 printed RFQs we saw were NBA same-game combos quote assembly does
   not price yet, and 7 were below the $5 size floor. The segment tables will fill at ~1,500 quoted
   decisions and ~40 quoted-sport prints per hour; the monitor keeps refitting.
4. **F(m) per request is tiny** (0.4-0.8% at m = 0). The optimiser is unaffected (argmax is scale-free)
   but the fill-rate knob needed a basis; it defaults to the printed-RFQ basis (above).
5. **Delta, sigma and the curse are per node, not per RFQ**; `curse_mult * sigma_rfq` carries the
   within-node sigma variation to the quote. Same-game combos' later fair is the product of leg fairs
   (the joint engine is the NFL session's); flagged by the `same_game` structure field.
6. Kalshi cross-check covers only combos with every leg matched: 51 of 160 NBA BUY prints at 1 min.

## Deferred (sent to the planning session for future-work.md)

- Per-RFQ delta from the live best competing quote (the RFQ feed only shows the print, not the losing
  quotes); a two-sided curse that includes our size relative to the print.
- Always-valid (mixture) confidence sequences for the block rule, only if the 7-day blocks ever cost
  measurable flow; the fixed 90% bound is the chosen trade-off.
- A joint-engine later fair for NFL same-game combos in the markout monitor.
