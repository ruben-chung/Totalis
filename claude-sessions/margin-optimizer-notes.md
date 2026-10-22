# Margin optimiser: notes

Written 2026-10-21 by the NBA session (totalis-c8) for the planning session (totalis-e0) and Ruben.
Brief: `claude-sessions/margin-optimizer-brief.md`. Code: `rfqbot/edge/` (owner nba). Tests: `tests/test_edge_*.py` (32).
Run: `python -m rfqbot.edge fit [--since-hours N]`, `report`, `wallets [--allow|--deny|--clear ID]`,
`monitor` (fit every `EDGE_REFRESH_SEC`, 300 s). Output `rfqbot/edge/margin_table.json`, state `data/edge.db`.

## What runs

- Paper quoting listener (step 0), started 2026-10-20 21:21 local on its own database:
  `RFQ_DB_PATH=data/rfq_quote.db RAW_LOG_DIR=none QUOTE_DECISIONS_DB=data/decisions.db rfqbot listen --quote`.
  `scripts/run.sh` keys its duplicate check on `rfqbot listen`, which the recording listener already
  matches, so it was started by hand with the same `nohup caffeinate` wrapper and logs to
  `data/logs/rfqbot_listen_quote.log`. It writes ~1,500 quoted decisions per hour (240k decisions per hour
  in total; the rest are skips, mostly `sport_not_quoted`). Its catalog sync is 429-limited while two
  listeners page the same endpoint; the cursor advances and no printed RFQ was lost to `unknown_market`.
- `scripts/run.sh rfqbot.edge monitor --since-hours 48`: a full fit every 5 min (markouts are incremental, a
  fit costs ~15 s plus ~0.2 s per new (game, minute) fair lookup).

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
6. `optimize.py`: `m* = argmax F(m) (m - curse(m))` over markups where at least `EDGE_MIN_WINS_AT_M` (2)
   printed decisions won and the fill-rate bound holds; every observed full cell is priced on the fill curve
   and delta of the node it resolves to but with **its own sigma** (a pooled node's mean sigma mixes NFL
   same-game cells at 3-8 pt with NBA cells at 0.3 pt and alone pushes m* to the grid edge). `MarginTerms`
   with `curse_mult = phi/Phi at z*` and `base = m* - curse_mult * sigma_cell`, so the quote-time rule
   `margin = base + curse_mult * (u_m + u_d) + info_premium + wallet_premium` gives exactly m* at the cell's
   sigma and charges the extra expected curse for a wider RFQ. `info_premium = max(0, -mean 30-min model
   markout)` pooled the same way (first node on the ladder with 30 markouts).
7. `table.py`: `margin_for(segment, requester=) -> MarginTerms(base, curse_mult, info_premium,
   wallet_premium, blocked, segment, level, n, source, block_reason)`; the lookup walks the ladder to the
   deepest node present; without a table it returns the default edge. Signature sent to the NFL session.

## Tables (fit of 2026-10-21 04:41 UTC, window 2026-10-20 22:42 -> 04:41 UTC)

Decisions: **11,435 quoted** (the NFL session's two 10-minute runs plus the continuous listener from 01:21
UTC). Prints in the quoted sports in the window: 279. **43 printed decisions, 13 would have won at our
actual quote.** The quoting listener covered ~3.5 h of the 7 h of `rfq.db`; of the printed RFQs it saw,
the ones not quoted were NBA same-game combos quote assembly does not price yet (17 of the first 40) and
sub-$5 sizes (7).

Full cells (n, printed, wins at our quote, node used, pooling level); 29 cells, the ones with prints:

| cell | n | printed | wins | node | lvl |
|---|---|---|---|---|---|
| nba\|2\|BUY\|disjoint\|s0 | 2,512 | 13 | 4 | itself | 0 |
| nba\|2\|BUY\|disjoint\|s1 | 1,114 | 3 | 3 | itself | 0 |
| nba\|2\|BUY\|disjoint\|s2 | 615 | 4 | 1 | itself | 0 |
| nba\|2\|SELL\|disjoint\|s0 / s2 | 404 / 319 | 1 / 2 | 0 / 1 | itself | 0 |
| nba\|3-4\|BUY\|disjoint\|s0 | 439 | 11 | 2 | itself | 0 |
| nba\|3-4\|BUY\|disjoint\|s2 | 20 | 1 | 1 | nba\|3-4\|BUY\|disjoint\|* | 1 |
| nba\|5+\|BUY\|disjoint\|s0 | 83 | 4 | 0 | itself | 0 |
| nfl\|2\|BUY\|disjoint\|s0 | 201 | 2 | 1 | itself | 0 |
| nfl\|3-4\|SELL\|disjoint\|s0 | 35 | 1 | 0 | itself | 0 |
| mixed\|2\|BUY\|disjoint\|s0 | 175 | 1 | 0 | itself | 0 |

Cells pooled upward: nba\|3-4\|BUY\|disjoint\|s1 (12), nfl\|3-4\|BUY\|disjoint\|s1 (17), nfl\|5+\|BUY\|disjoint\|s0
(15), mixed\|2\|SELL\|disjoint\|s1 (15) to their sigma parent; nba\|3-4\|SELL\|disjoint\|s0 (6) and the two NFL
2-leg SELL cells (1, 2) to `sport|*|SELL|*|*`. NFL same-game (2,054 decisions) and NFL 5+ legs (1,324) have
no print at all.

Fill curves and optimiser, nodes that fitted (`source = model`; pt = probability points):

| node | n | printed | F(0) | F(1pt) | F(2pt) | delta | sigma | m* | F(m*) | curse* | info | c_mult | base |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| nba\|2\|BUY\|disjoint\|s0 | 2,512 | 13 | 0.5% | 0.2% | 0.1% | 1.06 | 0.27 | 0.50 | 0.4% | 0.01 | 1.09 | 0.04 | 0.49 |
| nba\|3-4\|BUY\|disjoint\|s0 | 439 | 11 | 2.5% | 0.0% | 0.0% | 0.49 | 0.18 | 0.50 | 1.4% | 0.15 | 0.00 | 0.82 | 0.35 |
| nba\|2\|BUY\|disjoint\|* | 4,241 | 20 | 0.4% | 0.2% | 0.2% | 2.43 | 1.07 | 2.50 | 0.2% | 0.90 | 0.26 | 0.84 | 1.60 |
| nba\|3-4\|BUY\|disjoint\|* | 471 | 12 | 2.5% | 0.2% | 0.2% | 1.49 | 0.52 | 0.50 | 1.5% | 0.03 | 0.00 | 0.07 | 0.47 |
| nba\|*\|BUY\|*\|* | 4,795 | 36 | 0.7% | 0.2% | 0.2% | 1.86 | 1.00 | 2.50 | 0.2% | 1.24 | 0.00 | 1.24 | 1.26 |
| *\|*\|BUY\|*\|* | 9,721 | 39 | 0.4% | 0.1% | 0.1% | 1.78 | 2.60 | 10.00 | 0.0% | 8.92 | 0.00 | 3.43 | 1.07 |
| global | 11,435 | 43 | 0.4% | 0.1% | 0.1% | 1.81 | 2.40 | 4.50 | 0.0% | 3.89 | 0.00 | 1.62 | 0.61 |

Every SELL node and the NFL nodes have fewer than 5 printed decisions (`source = no_prints`): the loader
hands back the default edge for them. The two widest pooled nodes (`*|*|BUY|*|*`, global) sit at the grid
edge because their mean sigma (2.4-2.6 pt, dominated by NFL same-game decisions) makes the model curse
exceed any small markup; they only serve unseen cells (a new sport), every observed cell is priced with
its own sigma. F(m) is per request: 99% of quoted RFQs never trade, so F(0) is 0.4-2.5% and the fill-rate
bound `EDGE_MIN_FILL_RATE` is applied to F among printed RFQs by default (`EDGE_MIN_FILL_BASIS=print`).

Winner's curse, measured vs model (maker loss in pt). The model at m* says 0.01 pt for the s0 cell (sigma
0.27, delta 1.06) and 0.9-1.2 pt for the pooled NBA BUY nodes. The measured conditional move among the
decisions that would have won at m*:

| node | h | ref | n | measured | model |
|---|---|---|---|---|---|
| nba\|2\|BUY\|disjoint\|s0 | 15 | model / poly / kalshi | 8 / 8 / 6 | -4.96 / -5.15 / -6.65 | +0.01 |
| nba\|2\|BUY\|disjoint\|s0 | 30 | model / poly / kalshi | 9 / 9 / 5 | -3.71 / -3.74 / +1.70 | +0.01 |
| nba\|*\|BUY\|*\|* | 15 | model / poly / kalshi | 9 / 9 / 4 | -2.13 / -2.28 / -8.33 | +1.24 |
| nba\|*\|BUY\|*\|* | 30 | model / poly / kalshi | 8 / 8 / 3 | -1.35 / -1.23 / +11.94 | +1.24 |

Negative = the later fair moved in our favour. **The curse is not visible in this sample**: on the BUY
quotes we would have won, the combo fair fell afterwards (we sold YES, it got cheaper), by 1-5 pt, with
single prints at -16 and -24 pt (legs crossing into a game). The by-undercut bins (0.5 / 1 / 2 / 5 pt+)
hold 1-5 wins each and flip sign between bins. The Kalshi reference disagrees with Polymarket at 30 min
(+1.7 / +11.9 on 3-5 legs) where Kalshi books were stale. Nothing here contradicts the model; 8-9 wins
cannot measure a 1 pt conditional effect against 3-5 pt moves. The optimiser uses the model
(`EDGE_CURSE_SOURCE=model`); `measured` is available once the sample is a few hundred wins.

Markouts (maker P&L in pt after the print, model reference; Polymarket within 0.1 pt):

| horizon | BUY n | BUY mean (se) | SELL n | SELL mean (se) |
|---|---|---|---|---|
| 1 min | 218 | +0.07 (0.11) | 25 | -0.26 (0.15) |
| 5 min | 217 | +0.30 (0.23) | 25 | -1.38 (0.93) |
| 15 min | 213 | +0.29 (0.40) | 22 | -2.99 (1.51) |
| 30 min | 199 | +0.42 (0.50) | 16 | -1.46 (1.33) |

By sport over all horizons: nba BUY +0.28 (n 781), nba SELL -1.59 (n 81), nfl BUY +0.04 (n 64). Kalshi
reference (combos with every leg matched): nba BUY -0.02 (n 348), nba SELL -3.42 (n 26). BUY flow pays
the maker on average, SELL flow costs 1.4-3 pt at 5-15 min: the brief's direction (sell -0.25 at 15 min,
-0.57 at 30 min) with a larger size on tonight's 16-25 SELL prints. The informational premium is zero on
every BUY node; SELL nodes get it once they have 30 markouts (currently 16-25). Rows with no fair at the
print time (prints during a game) are final.

Wallets: 151 requesters scored on 213 observations, **0 blocked, 1 with a premium** (3 prints, mean -6.5 pt,
shrunk +0.08 pt, premium 0 after rounding). No wallet has 8 observations with a negative bound yet.

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
2. **Wallet profit per fill is scored on prints, not fills.** Paper mode has no fills and 13 counterfactual
   wins, so a wallet's observations are its prints in the quoted sports, each credited with our logged
   markup when we quoted that RFQ and the node's m* otherwise, plus the 30-min model markout.
   `n_wins` on the score records how many were real counterfactual wins.
3. **Counterfactual sample is thin** (43 printed decisions, 13 wins in 7 h): the quoting listener was up
   for half the window, NBA same-game combos are not priced yet, and the $5 floor drops some. The segment
   tables fill at ~1,500 quoted decisions and ~40 quoted-sport prints per hour; the monitor keeps refitting.
4. **The winner's curse could not be measured** (above): the measured conditional move is negative on
   8-9 wins per node with single-print outliers of 15-25 pt.
5. **F(m) per request is tiny** (0.4-2.5% at m = 0). The optimiser is unaffected (argmax is scale-free)
   but the fill-rate knob needed a basis; it defaults to the printed-RFQ basis (above).
6. **Pooled nodes needed per-cell sigma** (optimiser step 6); the first fit priced `*|*|BUY|*|*` at 10 pt
   for every cell under it. Per-cell pricing and the `EDGE_MIN_WINS_AT_M` guard were added before this fit.
7. Delta, sigma and the curse are per node, not per RFQ; `curse_mult * sigma_rfq` carries the within-node
   sigma variation to the quote. **The probe layer (`rfqbot.probe.analysis.probe_gap(rfq_id)`) will give a
   per-RFQ delta; not wired into `rfqbot/edge` yet** (the probe task did not touch this package).
8. Same-game combos' later fair is the product of leg fairs (the joint engine is the NFL session's).
9. Kalshi cross-check covers only combos with every leg matched: 98 of 218 BUY prints at 1 min.

## Deferred (sent to the planning session for future-work.md)

- Per-RFQ delta from `probe_gap` in the curse term and a two-sided curse that includes our size relative
  to the print.
- Always-valid (mixture) confidence sequences for the block rule, only if the 7-day blocks ever cost
  measurable flow; the fixed 90% bound is the chosen trade-off.
- A joint-engine later fair for NFL same-game combos in the markout monitor.
- Measured-curse objective (`EDGE_CURSE_SOURCE=measured`) once a node has a few hundred wins.
