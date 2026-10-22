# Stage 4 backtest harness: notes

Window: 2026-10-20 22:36 to 2026-10-21 02:36 UTC (4 h). Group `cmp1` in `data/backtest.db`; per-run JSON in `data/backtest_reports/`.
Settled against `market_resolutions` as refreshed 01:19 EDT (111,723 markets).

## 1. Shipped

- Package `rfqbot/backtest/`: `asof` (as-of readers, heartbeat), `lazy` (lazy game cache), `engine` (lock-step `Replayer`, `Strategy`), `config`, `feed`, `fills`, `store`, `settle`, `stats`, `report`, `compare`, `validate`, `__main__`.
- Tests: `tests/test_backtest_{world,asof,replay,settle}.py` (no-lookahead, determinism, fills into the book, void/unresolved settle and re-run, attribution sums, compare, validate, preload equivalence). Full suite: **433 passed**.
- Also changed (mine): `rfqbot/marginals/__init__.py` (readers may carry their own heartbeat via `.confirm`) and the `kalshi_prior` default `{"nfl": 2.0, "nba": 1.0}`. totalis-e0 commits these with the backtest.
- CLI (`python -m rfqbot.backtest ...`):
  - `run` / `compare --group G [--only a,b] [--report-only]`
  - `settle --group G`
  - `report --group G|--run NAME`
  - `validate`
  - `runs`
- Long jobs go through `scripts/run.sh`. The harness writes only `data/backtest.db` and reads live stores read-only.
- Design: one pass over the raw RFQ log with a pinned clock. Eleven strategies run in lock-step, each with its own Assembler, Inventory and Book, sharing one lazy game cache. As-of reads take the largest `at_ms <= t`. Fills use `rfqbot.edge.fills`.

## 2. Numbers

| Item | Value |
|---|---|
| Requests replayed | 2,275,720 (equals `rfq.db` rfqs, difference 0) |
| Other sport / unknown market | 2,228,154 / 682 |
| Evaluated / priced | 46,884 / 38,632 |
| Replay time | single run 1,445 s; 11-setting compare 5,331 s (machine load ~17) |
| Latency (base) | p50 4.8 ms, p99 33.6 ms, max 20.7 s (cold model build on the RFQ path) |
| Peak ES1 | 7,661 = 15.3% of 50k capital; es1 cap bound 1,834 times, outcome cap 448 |

Base run:

| | Count |
|---|---|
| Quoted | 19,236 |
| Printed | 52 |
| Win flag (ties count) | 21 |
| Strict flag (≥1 tick inside print) | 19 |
| Filled | 21 |

- Gross markup: +58.55 (21 fills, 9 games, 2 clusters, underpowered).
- Realized P&L: **+260.42** on 17 resolved combos, 5 games, 1 cluster (underpowered). 0 void; 4 fills (19%) unresolved, marked −19.33.
- Mark-to-model P&L (resolved plus marked): +241.09 ± 18.6, 2 clusters.
- Attribution: P&L +241.09 = pricing error +182.54 + margin +58.55. Margin by term: base +20.99, u_m +40.43, inventory −4.84, rounding +1.97.
- Markouts per share on model fair, maker (measured before re-settling; fills unchanged):
  - 1m +0.0030
  - 5m −0.0051
  - 15m +0.0116
  - 30m +0.0056
  - n 17–20 per horizon

Compare (Δ model P&L vs base; verdict is on the game-cluster SE):

| Setting | Quoted | Win | Filled | Markup | Realized (n) | Δ P&L | Max ES1 | Verdict |
|---|---|---|---|---|---|---|---|---|
| base | 19236 | 21 | 21 | 58.55 | 260.42 (17) | | 7661 | |
| indep | 19429 | 20 | 20 | 58.90 | 250.91 (16) | −9.66 | 7675 | underpowered |
| cal-ones | 19281 | 20 | 20 | 58.64 | 250.91 (16) | −9.51 | 7646 | underpowered |
| cal-0.75 | 19228 | 21 | 21 | 58.60 | 260.42 (17) | 0.00 | 7661 | underpowered |
| caps-off | 20008 | 19 | 19 | 58.77 | 335.80 (17) | +100.77 | **68490** (137%) | underpowered |
| edge-0 | 19901 | 39 | 39 | 45.01 | 223.45 (30) | −35.98 | 7534 | underpowered |
| edge-1pt | 19922 | 10 | 10 | 59.65 | 400.91 (7) | +143.43 | 7688 | underpowered |
| edge-2pt | 19653 | 1 | 1 | 8.32 | −39.66 (1) | −280.75 | 7696 | underpowered |
| table (in-sample) | 18959 | 13 | 13 | 44.65 | 181.82 (8) | −52.91 | 7625 | underpowered |
| shock-0.5 / shock-2 | 19236 | 21 | 21 | 58.55 | 260.42 (17) | 0.00 | 7661 | no effect (see 4) |

- Every comparison has 2 clusters, so none exceeds its clustered SE; the displayed SEs (±0.00 to ±0.06) are not meaningful. The only supportable statements are mechanical:
  - Fills fall steeply with the base edge: 39, 21, 10, 1 at 0, 0.5, 1 and 2 pt.
  - Removing the caps lets ES1 reach 137% of capital with the same fills.
- Validation against live `decisions.db` (earlier 4 h scratch run, NBA prior 2.0):
  - Fair: median |Δp| 6e-6; 90.8% within 0.5 pt; 93.9% within 1 pt; p99 5.3 pt; max 23 pt.
  - Median price differs by 0.5 pt because live uses the edge table while the replay base uses the stage-2 formula.
  - Decisions: 591 live-quoted vs replay `cap:es1`, 362 live `unknown_market` vs replay quoted. Median time offset 9 ms.

## 3. Corrections to the brief

- Trade rate is 0.0525% of requests, not 0.135%. SELL/BUY prints per request are about 1.2×, not 3.4× (0.063% vs 0.051%).
- Only 141 NFL/NBA prints exist in the window, so power is very low (21 fills, 2 clusters).
- "Shock set × for caps" is wrong: the shock set drives u_d only; the caps do not use it.
- The margin table was fitted at 02:14 UTC 2026-10-21 on this same flow, so the table run is in-sample. It is a mechanics check, not evidence.
- Catalog and `leg_matches` are end-of-recording, not as-of. Live `unknown_market` rows therefore differ from the replay.
- Stage-3 wiring "lands tonight" was already in the tree.
- Two `rfqbot listen` processes (pids 44511, 25961) were running; another session should check.

## 4. Findings

- **Shock scale is inert in this window.**
  - All 1,464 NFL combos with u_d > 0 are same-period (full-game) pairs.
  - The halves_corr and prior_rho shocks leave those unchanged; only the bootstrap seeds move them, and the scale does not touch seeds.
  - On cross-period pairs the scale does widen the interval (checked on the test game).
  - The shock-0.5 and shock-2 runs are therefore identical to base.
- **NBA same-game combos are unsupported** (7,286 skips), so u_d is 0 for all NBA fills. Margin attribution has no u_d term.
- **Realized vs marked flipped on re-settling.**
  - Before the resolutions refresh, all 21 fills were unresolved and marked at −30.84.
  - After it, 17 resolved to +260.42; the remaining 4 are marked −19.33.
  - The +182.54 pricing-error term is one cluster's luck, not an edge estimate.
- **Staleness is measured from the feed heartbeat**, not row age, because the stores are change-only.
- **Cold-start model builds** (max 20 s) land on the RFQ path in the replay. Live pre-warms them.
- Machine load slows runs several-fold (single 4 h run 1,445 s; the compare took 5,331 s).
- The NBA prior in the compare is 1.0. The scratch numbers in the validation bullet used 2.0.

## 5. Not done / v2

- Real as-of catalog and `leg_matches` (would remove the unknown_market disagreement).
- Calibration and edge table fitted out-of-sample (needs a second day of flow).
- NBA same-game pricing (7,286 skips) in the replay.
- Scale the bootstrap seeds in the shock comparison, or add cross-period combos to the window.
- Refit props with NBA `hist_prices` (mine, pending).
- Hourly rerun of `settle` as resolutions fill in (Data Engineer suggested an hourly resolve).

## Addendum: cmp2 (02:36 to 15:45 UTC 2026-10-21, 13.2 h, all 11 settings)

- Run: 56,220 RFQs evaluated, 58 games, 11,183 s (3.1 h; my estimate of 1.6-2.1 h was low, the load was 17-24). Peak RSS about 1.3 GB.
- The window ends 42 s before the network outage (15:45:42 to 21:24:55 UTC); the second segment is not run.
- Base: quoted 22,296, printed 112, win 26, strict 17, filled 26, gross markup +155.51, marked +114.73.
- Settlement at 2026-10-22 00:36 UTC: **all fills unresolved (100%)**. `market_resolutions` has none of these games yet because the last resolve pass ran before they ended, so realized P&L is 0. Re-run `settle --group cmp2` after the next `history resolve`.
- Clusters: the union-find over cross-game combos joins all 58 games into **1 cluster**, so every clustered SE is n/a and every verdict is underpowered. Game-clustering by cross-game combos is too coarse for a full day; cmp1 had 2.
- Compare (Δ marked P&L vs base): indep -2.25, cal-ones 0.00, cal-0.75 0.00, caps-off +17.91 (max ES1 57,299 = 115% of capital), edge-0 +4.92 (60 fills), edge-1pt +2.69 (5 fills), edge-2pt -80.54 (1 fill), table -73.72 (19 fills), shock x0.5 / x2 0.00 (inert, same reason as cmp1).
- Table is in-sample: `margin_table.json` was refit at 14:11 UTC inside the window. Post-14:11 slice (table fitted before these fills): table 12 fills, marked +11.67; base 5 fills, +3.80 (5 fills, 4 games). Before 14:11 the base run has 21 fills, +110.93. Slice n is too small for any conclusion, and none of it is resolved.
- Correction: the 3,618 prints in the message are all sports; NFL/NBA prints in the quoted window are 112 (about 91 for the table's quoted set).
