# Margin table wired into quote assembly (2026-10-21)

Per the planning session's request after `docs/margin-optimizer-brief.md` (NBA session's
`rfqbot.edge`). Changes in `rfqbot/quoting/{assemble,config,decisions}.py`, `rfqbot/__main__.py`;
tests `tests/test_quoting_edge.py` (3). Inventory and caps unchanged.

## Shipped

* `Assembler._margin_terms`: segment `Segment.of(sport, n_legs, direction, same_game, u_m + u_d,
  cuts)` with sport = the legs' slug sport (`mixed` across sports), `same_game` = any cluster with
  more than one leg, cuts from `load_edge_config().sigma_cuts`; `margin_for(seg, requester,
  table=...)` on the table held in memory.
* Margin rule: a fitted node (`source` model / measured) sets `e_b = terms.base` and
  `margin0 = terms.margin(u_m + u_d)` = base + curse_mult x (u_m + u_d) + info + wallet premiums.
  A `no_prints` / `no_data` node or no table keeps the stage 2 margin
  (`QUOTE_BASE_EDGE + k_m u_m + k_d u_d`) plus the premiums the node carries. `terms.blocked` ->
  `skipped:wallet_blocked` with the block reason. The inventory skew `e_i` is added after, as before.
* Reload off the hot path: `Assembler.reload_edge_table()` (`load_table(path, max_age_s=0)`) is
  registered on `GameCache.on_refresh`, so a refit by `python -m rfqbot.edge monitor` is picked up
  within a refresh period without a restart; the RFQ path never stats the file. With no table file
  the assembler answers the default itself (`margin_for` would otherwise fall back to the default
  path).
* Decision row: `edge_segment` (node key that priced it, or the segment when default), `edge_level`,
  `edge_source`, `edge_n`, `curse_mult`, `info_premium`, `wallet_premium` (migrated in place).
* Config: `QUOTE_EDGE` (default on; `0` = stage 2 formula), `QUOTE_EDGE_TABLE` (path override).

## Numbers

Offline, on the 531 quotes of the caps-live run (`data/decisions_caps3.db`, same ten minutes of
flow, table fitted 2026-10-21 00:49):

| item | value |
|---|---|
| node resolution | 133 fitted (`model`), 398 `no_prints`; level 0 (exact cell) 519, level 1 12; 0 blocked |
| margin before (stage 2 + e_i) | mean 0.0480, p10 0.0074, p50 0.0425, p90 0.0860 |
| margin after | mean 0.0499, p10 0.0079, p50 0.0460, p90 0.0860 |
| after - before | mean +0.0019, max +0.0073; 222 wider, 0 tighter, 309 unchanged |

Live 10-minute run with the table wired (2026-10-20 22:16-22:26 local, isolated db copy, caps
on, thinner late-evening flow; `data/decisions_edge.db`):

| item | value |
|---|---|
| RFQs / quoted | 103,934 / 212 (66 combos, 34 requesters); 0 `wallet_blocked` (no wallets in the table yet) |
| node resolution on quoted | 35 fitted (`model`, all `nba\|2\|BUY\|disjoint\|s0`), 177 `no_prints`; level 0: 203, 1: 5, 3: 1, 4: 3 |
| margin on quoted | mean 0.0524, p10 0.0129, p50 0.0450, p90 0.1052 |
| the fitted cell | base 0.0049 + 0.049 x sigma + info premium 0.0095 = 0.0144 vs stage 2 0.0072 |
| caps | ES1 at the cap (15.0% at the end, max 15.19%); skips cap:es1 190, cap:outcome 14; cuts Kelly 20, es1 20, outcome 13 |
| latency | all p50 0.74 ms, p99 5.1 ms; quoted p50 7.7 ms, p99 53 ms (machine busy with the edge monitor and the backtest session; `margin_for` itself is 4 us) |
| reload | the table was refitted during the run (file mtime 22:25); decision rows carry the earlier fit's node values (base 0.004858) and the file now holds the later one (0.004868), so the refresh-thread reload is what fed the quotes |

## Corrections to the brief

* `no_prints` nodes carry `base = default edge, curse_mult = 0`; taken literally they would drop
  the uncertainty terms (u_m + u_d) from the margin for the 75% of quotes that resolve there. I keep
  the stage 2 margin for those nodes and use the fitted formula only for `model` / `measured`
  nodes. Say so if the optimiser intends curse_mult 0 to mean "no uncertainty charge".
* `margin_for(table=None)` loads the default table path, so "no table" cannot be expressed through
  it; the assembler short-circuits to `MarginTerms(base_edge, 0, 0, 0)` when its reload found none.

## Findings

* The global node `*|*|*|*|*` is fitted (base 0.0102, curse 1.02), so every segment resolves to
  some node; the share "fitted vs default" is really "fitted at level 0 vs pooled vs no_prints".
* The fitted nodes widen margins slightly on this flow (curse_mult near 1 plus base ~0.01 vs
  0.005); nothing tightens because the no-prints fallback keeps the stage 2 margin.

## Not done / v2

* No `measured` nodes exist yet; the branch is the same as `model`.
* The wallet premium is added for fallback nodes too (the table's wallet terms are per requester,
  not per node); confirm that is the intended reading.
