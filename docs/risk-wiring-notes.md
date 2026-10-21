# Caps, sizing and inventory skew in quote assembly (stage 3 part 3, 2026-10-20)

Built against `docs/risk-wiring-brief.md`. New `rfqbot/quoting/inventory.py`; changes in
`rfqbot/quoting/{assemble,cache,config,decisions}.py`, `rfqbot/risk/{book,caps,exposure}.py`,
`rfqbot/__main__.py`; tests `tests/test_quoting_inventory.py` (7) plus updated `tests/test_risk_caps.py`.
On by default with `listen --quote`; `QUOTE_RISK=0` restores stage 2 behaviour.

## Shipped

* **Live book** (`quoting/inventory.py`, `Inventory`): a `risk.Book` of our quotes. A quote is
  pending at its quoted size from the moment it goes out (`on_quoted`) until
  `submission_deadline + acceptance 10 s + last look 1 s + EXPIRY_GRACE_MS` (the listener's rule,
  `horizon_ms`), filled at the trade size when an execution or a `SELECTED/EXECUTING/SETTLED`
  status is in the store, settled from `market_resolutions`. Store polling, expiries, fills and the
  exposure rebuild run in `refresh()` on the cache's worker thread (`GameCache.on_refresh` hook,
  every `QUOTE_RISK_REFRESH_SEC` = 2 s), never on the RFQ path. `Inventory.load(decisions_db)`
  rebuilds the pending book after a restart from the logged `expires_ms` and the store's statuses.
* **Hot path** (`Inventory.assess`): the candidate's per-share exposure is derived on the book's
  models and common random numbers (`Exposure.derive`); outcome deltas are one matrix-vector
  product per game (`outcome_matrix` cached on the model, warmed on the refresh thread), the
  per-game worst loss is closed form, ES1 is linearised on the book's tail and checked exactly
  (bisection only when the linear size fails). When a quote goes out its per-share vectors are
  absorbed into the hot exposure at the quoted size (`Exposure.absorb`), so the next RFQ for the
  same combo already sees it; the refresh thread replaces the exposure with a clean rebuild.
* **Sizing** (`assemble.py` step 6): `size = min(requested, tier cap, kelly, caps.allowed_size)`,
  `kelly = QUOTE_KELLY_FRACTION x capital x edge / (p(1-p))` with edge = margin net of the inventory
  charge (`margin - e_i`, the risk-adjusted edge, so a hedge quoted at fair still sizes on the tail
  it removes). Zero allowed size, or below `min_size_usd`, is `skipped:cap:<binding_cap>` with the
  headroom in the reason and in `headroom_json`. A cut is tagged `cut:kelly` / `cut:cap:<cap>` in
  the reason and `binding_cap` in the row.
* **Inventory skew**: `e_i = QUOTE_K_INV x dES1/ds x usage + QUOTE_INVENTORY_SKEW`, where dES1/ds is
  the candidate's mean loss per share in the book's 1% tail (positive = adds to the tail) and
  usage is the book's largest |delta| / outcome limit over the outcomes the candidate moves
  (clipped at 2). `QUOTE_K_INV` default 0.04: a quote fully correlated with the tail (loss 0.5 per
  share) at the outcome cap's midpoint adds 0.01. `margin = max(0, margin0 + e_i)`: a hedge
  tightens down to fair and never through it.
* **Repeated requests**: `pending_same_combo` / `pending_requesters` per `condition_id` in every
  decision row and in the reason (`pending=15/5req`).
* **Decisions log**: new columns `requester, condition_id, deadline_ms, expires_ms, requested_shares,
  kelly_size, allowed_size, binding_cap, headroom_json, d_es1_per_share, pending_same_combo,
  pending_requesters, book_json` (positions, pending, notional, ES1, ES1 fraction at decision time);
  `ALTER TABLE` migration on open, old rows keep NULLs. `Book.from_decisions(assume_filled=False)`
  now uses the logged `expires_ms`.
* Config: `QuoteConfig.risk, kelly_fraction, k_inv, risk_refresh_sec` (env `QUOTE_RISK`,
  `QUOTE_KELLY_FRACTION`, `QUOTE_K_INV`, `QUOTE_RISK_REFRESH_SEC`); caps from `RISK_*` as before.
* `rfqbot/config.py`: an empty `RAW_LOG_DIR` now falls back to `./data/raw` (`"none"` still turns
  the raw log off); `tests/test_config.py`.

## Numbers: 10-minute paper run with caps live (2026-10-20 21:57-22:07 local, isolated db copy, unauthenticated feed)

Default caps (capital $50k; outcome 5%, game worst loss 10%, ES1 15%; notional caps off), Kelly
fraction 0.25, `k_inv` 0.04. Three runs were made; the first two found a price-above-one crash
(19 `risk_error` rows) and a slow hot path (quoted p99 322 ms), fixed before this one.

| item | value |
|---|---|
| RFQs seen / quoted | 107,363 / 531 (108 distinct combos, 66 requesters) |
| skipped by a cap | `cap:es1` 120, `cap:outcome` 30 (game, sport, day never bound) |
| quotes cut below request | Kelly 95, `cap:es1` 57, `cap:outcome` 15; 364 uncut |
| e_i over quoted rows | min -0.0115, p10 -0.0006, p50 0.0000, p90 +0.0231, max +0.0320; 155 negative (hedges), 69 zero, 307 positive |
| latency, all RFQs | p50 0.48 ms, p99 2.48 ms, max 139 ms (stage 2: p50 0.63, p99 2.93) |
| latency, quoted RFQs | p50 2.86 ms, p99 19.9 ms, max 40 ms; `assess` p50 2.3 ms, p99 19 ms |
| refresh thread | book rebuild 16 ms every 2 s; 85 refreshes, 512 expiries |
| live book ES1 | at the cap (max 15.16% of capital = $7,579) whenever flow repeated; 8.4% at the end |
| live book at the end | 19 pending, $5,641 collateral at risk; `python -m rfqbot risk` on the log (pending only): 14 positions, ES1 $2,437 = 4.9%, caps usage outcome 0.69 / game 0.39 / ES1 0.32, worst game NE@CHI -$1,968 |
| largest pending stack per combo | 7 quotes from 2 requesters (was 110 with no caps) |
| assume-filled stress view | 531 positions, $212,631 at risk, ES1 $164,563 = 329% of capital |

The assume-filled number is what the brief's first run measured: every quote over the ten minutes
treated as filled and held. With caps the *live* book never exceeds the ES1 cap, but a pending
quote expires 31 s after its deadline and frees its headroom, so the same combo can be quoted again
every half minute. Without execution updates (unauthenticated feed) no quote ever fills, so the
caps bound the exposure at any instant, not the cumulative quoted notional. Once fills arrive they
stay in the book until resolution and the caps bind for the rest of the day; the stress view is
the upper bound if every quote filled.

Top deltas of the end-of-run live book: NE@CHI total under 50.5 +$1,737, NE total under 23.5
+$1,638, CHI -3.5 covers +$1,606 (the same combo family as the first exposure run, now capped).

## Findings

* The ES1 cap binds far more than the outcome cap (120 vs 30 skips, 57 vs 15 cuts): pending
  quotes across many NBA factors add up in the tail before any single outcome reaches 5%.
* Kelly is the most common cut (95): at a 1-2 point margin and p near 0.5, 0.25 x 50k x 0.015 /
  0.25 is about 750 shares, under the $500-per-quote tier cap at low prices.
* The skew is small in practice (p90 +0.023, max +0.032) because usage of the outcome cap rarely
  exceeds 0.7 before ES1 cuts the size; hedges get up to -0.0115 and are quoted at fair when the
  charge exceeds the base margin.
* The first run's `risk_error` rows were sells priced above 1.0 (fair near 1 plus margin); the
  safety check now runs before the candidate is built, and `_price` clamps at 1.0.
* Latency on quoted RFQs is dominated by the engine's `joint()` (1.4 ms) plus `assess` (2.3 ms);
  the first assess after a model rebuild was 300 ms until the indicator cache and the outcome
  matrix were warmed on the refresh thread.

## Corrections to the brief

* `marginal_risk()` is not called on the hot path: its ES1 slope is computed inside `assess` from
  the book's cached tail indices (same number, no second Monte Carlo).
* Kelly uses `margin - e_i` as the edge, not the margin earned: with the margin itself, a hedge
  quoted at fair (margin 0) would size to zero and be skipped, the opposite of what the skew wants.
* The cap check runs on the hot path after all (closed forms, 2-3 ms), with pending exposure
  absorbed per quote; the refresh thread does the rebuild, not a per-game headroom table. The
  brief's "only re-check when a quote would move it" is unnecessary at this cost.

## Findings

See the run section.

## Not done / v2

* No live executions reach an unauthenticated listener, so the fill path (trade size, settlement)
  is exercised only by `tests/test_quoting_inventory.py::test_refresh_expires_fills_and_rebuilds`.
* Cross-sport combos get `sport` of the first leg for the sport-notional cap.
* The edge loader from the NBA session (`rfqbot.edge.margin_for(Segment, requester)`) is not wired
  yet; it replaces `base_edge` and `k_m = k_d = 1` with `terms.margin(u_m + u_d)` and a `blocked`
  skip when the planning session schedules it.
