# Brief: quote assembly (RFQ -> PaperQuote)

Written 2026-10-20 by the planning session (`totalis-e0`). Owner: Ruben.
Send a summary to `totalis-e0` when done. For the NFL session (owner of `rfqbot/pricing`).

## What it is

The handler passed to `Listener(on_rfq=...)`. It receives an `RfqRequest` and the `Store`
and returns a `PaperQuote(price_e6, size_e6, source, reason)` or `None`, inside the
~385 ms submission window. It is the first point at which the bot prices anything, so it
must be explainable line by line and must log every decision with its components, because
the backtest (stage 4) attributes P&L to those components.

## Components it composes (all exist or are in flight)

- Catalog: `Store.get_market(position_id)` rows (slug, condition_id, title, outcome).
- Independence classifier: `rfqbot.clusters.cluster(legs) -> Clustering` and
  `combo_probability(clustering, p_leg, joint_fn)` (NBA session, in flight, brief
  `docs/independence-classifier-brief.md`). Code against that interface; stub it locally
  under `rfqbot/quoting/_cluster_stub.py` until it lands, then delete the stub.
- Marginals: `rfqbot.marginals.marginal_for_leg(mq, match, kq, sport=, now_ms=, cfg=)` and
  `compute_game(prefix, at_ms, ...)` (done; `docs/marginal-layer-notes.md`). Kalshi quotes
  arrive automatically once `rfqbot.kalshi_books` lands.
- Joint engine: `rfqbot.pricing.joint.build_game(quotes) -> GameModel`, `GameModel.joint(specs)
  -> JointQuote` (yours). Keep one `GameModel` per game prefix in a cache, rebuilt when
  `ladders.game_markets` changes materially or every N seconds, never inside the RFQ path
  if a fresh one exists; a cold game may be built on first sight if it fits in the window,
  otherwise skip that RFQ and warm it for the next poll (requesters re-poll ~1 Hz).
- Quotes: `rfqbot.ladders.game_markets(prefix)` for Polymarket books.

## Combo semantics (pin these first, with evidence, in `rfqbot/quoting/semantics.py`)

From the Polymarket Combos docs and our recorded `RFQ_REQUEST` / `RFQ_TRADE` / quote
frames: what the combo YES position pays (all legs' positions pay?), what `side` (YES/NO)
and `direction` (BUY/SELL) mean from the requester's point of view and therefore which side
the maker takes, what `requested_size` means for `unit = shares` vs the notional unit, and how
`price_e6` on our quote is interpreted (price of the YES or of the requested side). Write the
mapping as code with a docstring citing the evidence. The NBA session was asked to do this
inside the classifier brief; it is now yours, and it has been told.

## Price construction (keep every term separate and logged)

1. `p_legs`: a `Marginal` per leg. Any leg with tier `skip`, or `p is None` -> no quote,
   reason `leg:<flag>`.
2. `clustering`; if not certified and any cluster is `no_game_key` -> no quote
   (`unpriceable_dependence`). Multi-leg same-game clusters -> `GameModel.joint`; if the
   model returns unsupported legs -> no quote (`unsupported:<family>`), v1 (a conservative
   default coupling can come later).
3. `p_fair` = product over clusters (single legs: `Marginal.p`; multi-leg: `JointQuote.p_joint`).
4. Marginal-uncertainty term `u_m`: propagate leg errors through the product
   (relative errors in quadrature, converted back to probability points).
5. Dependence-uncertainty term `u_d`: half the width of each multi-leg cluster's `[lo, hi]`
   scaled through the product; zero for certified-independent combos.
6. Base edge `e_b`: config `QUOTE_BASE_EDGE` (default 0.02), to be calibrated by the backtest.
7. Inventory skew `e_i`: 0 in v1, but the slot and the log column exist (stage 3 fills it).
8. Margin `m = k_m * u_m + k_d * u_d + e_b + e_i` with config multipliers (defaults 1.0).
   Quoted price for the side the maker is taking, per semantics: if the maker is selling the
   outcome, `p_fair + m`; if buying, `p_fair - m`; clipped to the tick and to [0.001, 0.999].
9. Size: `min(requested, QUOTE_MAX_SIZE_USD / price, size_cap(tier, n_legs))` where
   `size_cap` shrinks with the worst leg tier and with leg count (config table). Below
   `QUOTE_MIN_SIZE` -> no quote.
10. `reason`: one line, e.g.
    `fair=0.231 (indep 2 clusters) u_m=0.012 u_d=0.000 base=0.020 inv=0 -> ask 0.263 x 400;
    legs: nfl|...|spread MIN -7.5 p=0.485±0.006 ok | nba|... total over p=0.476±0.011 wide`.

## Logging

Every RFQ seen by the handler gets a row in a new sqlite file `data/decisions.db` (yours):
rfq_id, at_ms, n_legs, clusters json, certified, per-leg (position_id, p, err, tier, flags,
kalshi_confidence), per-cluster (game_key, p, lo, hi, source), p_fair, u_m, u_d, e_b, e_i,
quoted price, size, decision (`quoted` | `skipped:<reason>`), latency_ms. This table is the
backtest's input; make it cheap to write (one insert, JSON columns fine).

## Latency budget

Target under 50 ms per RFQ at the p99 with warm caches: marginals are per-leg lookups on the
latest book rows (cache the latest `game_markets` result per prefix, refreshed by a background
task, not per RFQ), the engine pair evaluation is 0.5 ms. Measure and report p50/p99 on the
live feed in paper mode.

## Deliverables

`rfqbot/quoting/` (`semantics.py`, `assemble.py`, `cache.py`, `decisions.py`, `config.py`),
wiring in `rfqbot/__main__.py listen --quote` (off by default; `PAPER_TRADING` stays true),
tests: semantics against recorded frames; a full assembly on synthetic legs for each path
(certified product, same-game cluster via a synthetic GameModel, each skip reason); size caps;
the decisions row round-trips. Run it live for 10 minutes in paper mode and report: RFQs seen,
quoted, skipped by reason, latency p50/p99, and ten sample reasons.

## Ownership

You own `rfqbot/quoting/*`, `rfqbot/pricing/*`, `rfqbot/__main__.py`, `rfqbot/store.py`,
`tests/test_quoting_*.py`. Marginals belong to totalis-48, books/ladders/history to the Data
Engineer session, kalshi_books and clusters to the NBA session. Not a git repo.
