# Quote assembly: what was built and what the first live runs say (2026-10-20)

Built against `docs/quote-assembly-brief.md`. Package `rfqbot/quoting/` (`semantics.py`,
`config.py`, `cache.py`, `assemble.py`, `decisions.py`, `_cluster_stub.py`), wiring
`python -m rfqbot listen --quote [--decisions-db PATH]`, tests `tests/test_quoting_*.py`.

## Combo semantics (rfqbot/quoting/semantics.py, with the evidence)

* Combo YES pays 1 iff every leg's selected position wins (docs: "a combinatorial YES position
  pays out only if every leg wins"); NO pays otherwise; YES + NO = 1 pUSD per share.
* `side` is always YES (docs; 514k recorded RFQs, 674 trades: all YES). `direction` is the
  requester's: BUY -> maker sells YES at `price_e6` (or buys NO at 1 - price from collateral);
  SELL -> maker buys YES.
* `requested_size`: `notional` (pUSD, BUY only; "maximum collateral budget, including fees"),
  `shares` (SELL only). Recorded: 544 BUY trades print `shares x price / notional` in
  0.917-1.000 (median 0.965, the fee allowance); 143 SELL trades print 0.997-1.000 x the
  requested shares. Share size for a BUY at our price: `floor(value_e6 * 1e6 / price_e6)`.
* Our quote: `price_e6` = pUSD per YES combo share for both directions; `size_e6` in share base
  units. BUY quotes above 0.90909 are rejected by the venue (safety threshold). Tick size is
  undocumented; we round to 0.001 away from fair.
* The ~0.999 SELL prints: every leg of every such combo is a game played on the recording day
  (27/27 legs on 2026-10-20, e.g. Liverpool / Roma / Man City to win, already decided). They are
  liquidity exits of effectively settled combos, not mispricings.

## Pipeline (assemble.py), every term logged

1. legs: `Store.get_market` -> `rfqbot.clusters.cluster(rows)`; per-leg marginal from the game
   cache (`GameCache`, refreshed by a background task via `rfqbot.marginals.compute_game`, so the
   marginal layer's confirmation-based staleness applies). Skips: `leg:unknown_market`,
   `leg:sport_not_quoted` (only sports with ladders in the book recorder: nfl, nba),
   `leg:cold_game` (first sight: tracked, refreshed within a second, quoted on the next poll),
   `leg:<flag>` when `p` is None or tier `skip`.
2. clusters: uncertified with a `no_game_key` leg -> `unpriceable_dependence`; `same_market` /
   `same_group` -> p = 0 (quoted as a sale at the margin, tagged `[impossible]`); same-game
   NFL -> `GameModel.joint`, priced on its calibrated `p_cal` with the model-error-widened interval (`unsupported:<family>` when a leg is not modeled,
   `cold_model` when the model is not built yet); same-game NBA -> `unsupported:nba_same_game`.
3. `p_fair` = product over clusters; `u_m` = p_fair x sqrt(sum (err_i / p_i)^2) over all legs;
   `u_d` = p_fair x sqrt(sum ((hi - lo) / 2 / p_c)^2) over joint clusters (0 when certified);
   `m = k_m u_m + k_d u_d + e_b + e_i` (defaults 1, 1, 0.005, 0; the flow clears BUY accumulators at +0.5..0.75 pt over the product of mids, so 2 pt would sit outside every fill).
4. price: maker sells -> ceil((p_fair + m) / tick); maker buys -> floor((p_fair - m) / tick);
   clipped to [0.001, 0.999]; BUY requests above the 0.90909 safety threshold are skipped
   (`price_above_safety`), SELL requests whose bid would be below 0.001 are skipped
   (`price_below_min`). Hard invariants (asserted): never sell below `p_fair`, never buy above it.
5. size: `min(requested, max_size_usd x tier_mult(worst leg) x legs_mult(n) / price)`;
   below `min_size_usd` -> `size_below_min`.
6. round trips: the same requester on the same combo in the opposite direction within 10 s is
   tagged `[round_trip]` in the reason (planning session evidence: quotes below independence
   fair are harvested within ~3 s).

## Latency design

The RFQ handler never touches the network or sqlite: it reads the cache dict. The joint model's
six shocked variants are pre-warmed in the refresh thread (first `joint()` on a cold model is
~0.5 s, warm pairs ~1 ms). Game refresh (quotes + Kalshi + marginals + model) is ~1.4 s for an
NFL game with 622 legs and runs in a worker thread every `QUOTE_REFRESH_SEC` (20 s).

## Live paper runs (2026-10-20 evening, isolated db `data/quote_run.db`, decisions in `data/decisions.db`)

Run 2 (final code: base edge 0.005, confirmation-based staleness, shocks pre-warmed), 10 minutes
from 18:52: **84,330 RFQs seen, latency p50 0.63 ms, p99 2.93 ms, max 95 ms** (handler only, in
the listener's event loop; budget was 50 ms at the p99). 42 games cached, 624 refreshes and 94
joint-model builds in the worker thread, no errors.

| decision | count |
|---|---|
| skipped:leg:sport_not_quoted (soccer, tennis, ... no ladders recorded) | 80,821 |
| quoted | 1,407 |
| skipped:size_below_min (requested notional under $5 at our price) | 1,287 |
| skipped:unsupported:nba_same_game (no NBA joint engine in v1) | 533 |
| skipped:unpriceable_dependence (a leg with no game key) | 139 |
| skipped:leg:cold_game (first sight; quoted on the next poll) | 80 |
| skipped:leg:unknown_market | 56 |
| skipped:leg:inplay / leg:wide | 7 |

Of the 1,407 quotes: 1,394 two-leg, 11 three-leg, 2 single; 1,352 certified independent, 55
same-game NFL pairs through the joint engine (u_d median 0.003); 914 asks (maker sells YES),
493 bids; median margin 0.011 (u_m median 0.006 + base 0.005). 610 quotes carry `[round_trip]`:
the same requester polling the same combo in both directions within 10 s, which is what the
flow looks like (bots poll BUY and SELL alternately), so the tag is a descriptor, not a filter.
Run 1 (base edge 0.02, same window one run earlier): 82,932 seen, p50 0.52 ms, p99 2.09 ms,
1,229 quoted; the lower base edge moved asks from ~+2.6 pt to ~+1.1 pt over fair.

Ten sample reasons (run 2):

```
fair=0.3759 (indep 2) u_m=0.0108 u_d=0.0000 base=0.005 inv=0 -> ask 0.392 x 128 [round_trip]; legs: nba|2026-10-20|PHI@NYK|FG|moneyline|-|-|NYK|Knicks p=0.627±0.016 disagree | nba|2026-10-21|CHA@BKN|FG|spread|CHA|-1.5|BKN|Nets p=0.600±0.008 ok
fair=0.3759 (indep 2) u_m=0.0108 u_d=0.0000 base=0.005 inv=0 -> bid 0.360 x 125 [round_trip]; legs: (same legs)
fair=0.3069 (indep 2) u_m=0.0078 u_d=0.0000 base=0.005 inv=0 -> ask 0.320 x 16; legs: nba|2026-10-20|OKC@SAS|FG|moneyline|-|-|SAS|Spurs p=0.490±0.005 ok | nba|2026-10-20|PHI@NYK|FG|moneyline|-|-|NYK|Knicks p=0.626±0.015 disagree
fair=0.2732 (clusters 1, 1 joint) u_m=0.0037 u_d=0.0033 base=0.005 inv=0 -> ask 0.286 x 1748; legs: nfl|2026-10-23|NE@CHI|FG|spread|CHI|-3.5|NE|Patriots p=0.546±0.005 ok | nfl|2026-10-23|NE@CHI|FG|total|-|50.5|over|Over p=0.510±0.005 ok
fair=0.3755 (indep 2) u_m=0.0102 u_d=0.0000 base=0.005 inv=0 -> ask 0.391 x 102; legs: nba|2026-10-20|PHI@NYK|FG|moneyline|-|-|NYK|Knicks p=0.626±0.015 disagree | nba|2026-10-21|CHA@BKN|FG|spread|CHA|-1.5|BKN|Nets p=0.600±0.008 ok
fair=0.6030 (indep 1) u_m=0.0112 u_d=0.0000 base=0.005 inv=0 -> ask 0.620 x 31; legs: nfl|2026-10-23|NE@CHI|FG|moneyline|-|-|CHI|Bears p=0.603±0.011 disagree
fair=0.3015 (indep 2) u_m=0.0039 u_d=0.0000 base=0.005 inv=0 -> ask 0.311 x 307; legs: nba|2026-10-20|OKC@SAS|FG|moneyline|-|-|SAS|Spurs p=0.492±0.005 ok | nba|2026-10-20|PHI@NYK|FG|moneyline|-|-|NYK|Knicks p=0.613±0.005 ok
fair=0.1872 (indep 2) u_m=0.0031 u_d=0.0000 base=0.005 inv=0 -> ask 0.196 x 1090; legs: nba|2026-10-20|OKC@SAS|FG|moneyline|-|-|SAS|Spurs p=0.492±0.005 ok | nba|2026-10-20|PHI@NYK|FG|moneyline|-|-|PHI|76ers p=0.381±0.005 ok
fair=0.0000 [impossible] (clusters 1) u_m=0.0000 u_d=0.0000 base=0.005 inv=0 -> ask 0.005 x 1000; legs: nfl|...|FG|moneyline|-|-|CHI|Bears | nfl|...|FG|moneyline|-|-|NE|Patriots   (same-market YES+YES, synthetic)
skip unpriceable_dependence: a leg has no game key; legs: ...
```

## Things to revisit

* `size_below_min` is the second largest bucket: most notional requests are $1-5 polls; the
  $5 floor is a config knob (`QUOTE_MIN_SIZE_USD`).
* The joint model rebuilt 94 times in 10 minutes for one NFL game because thin far-ladder mids
  flicker past `QUOTE_MODEL_REBUILD_MOVE` (0.005); harmless in the worker thread (0.6 s each)
  but a smarter change detector (informative legs only, debounce) would cut it.
* NBA same-game pairs (533 in 10 minutes, 38% of the quotable flow) need an NBA joint engine or
  a conservative default coupling.
* Only sports with recorded ladders are quoted; 96% of RFQs are other sports (soccer, tennis,
  esports) and are skipped at the first leg.
