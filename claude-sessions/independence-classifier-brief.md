# Brief: leg independence classifier + cluster pricing

Written 2026-10-20 by the planning session (now `totalis-e0`). Owner: Ruben.
Send a summary to `totalis-e0` when done. Second task for `totalis-c8`, after the Kalshi feed.

## Why

In the 75 s live sample, 97.2% of two-leg combos were legs from different games, 2.6% same
game, 0.1% a leg with no game key (two Iran politics markets, strongly dependent), 0.1% different
games sharing a team. Cross-game legs are independent for pricing, so the fair price is the
product of the marginals. Same-game legs go to the joint engine (`rfqbot/pricing`, being built
by `totalis-47`). This module decides which is which, deterministically, and assembles the
combo price from per-cluster probabilities.

## Classes (ordered; the first that applies wins)

1. `same_market`: same `condition_id`. The YES and NO of one market together are impossible
   (joint 0); the same position twice is just that leg.
2. `same_group`: siblings in a mutually exclusive group of one game. Detect by a per-sport
   registry of exclusive-group slug families: NFL `exact-margin-*`, `highest-scoring-quarter-*`;
   soccer `exact-score-*`, `halftime-result-*`, `second-half-result-*`; NBA none yet. Joint 0.
3. `same_game`: same game key. Goes to the joint engine; pairwise logic inside a game (over vs
   under at one line, two covers of one team) is the engine's job, not this module's.
4. `no_game_key`: either leg has no game prefix. Never certified; the combo is flagged
   `unpriceable_dependence` and quote assembly skips it.
5. `shared_team_diff_game`: different games, same sport, overlapping team codes. Priced as
   independent, tagged for stage 3 exposure netting.
6. `disjoint`: certified independent.

Game key: `LegSpec.game_key` via `rfqbot.sports.registry.sport_for_slug` when the sport is
registered; otherwise the generic slug prefix `{league}-{a}-{b}-{YYYY-MM-DD}` (100% of team
sports). Add the tennis doubles shape (`atp-doubles-{a}-{b}-{date}-...`) as a second pattern.
Anything that matches neither is `no_game_key`. Shared-team detection only for registered
sports (team codes) plus the generic slug codes.

## Deliverables

- `rfqbot/clusters.py` (you own it): `classify_pair(a, b) -> PairClass`,
  `cluster(legs) -> Clustering` where legs are catalog rows (position_id, condition_id,
  slug) and `Clustering` has `clusters: list[Cluster(game_key | None, legs)]`,
  `pair_classes`, `certified_independent: bool` (every cluster has one leg and no
  `no_game_key`), `flags`.
- `combo_probability(clustering, p_leg: dict[position_id, float], joint_fn) -> ComboProb`:
  product over clusters, where a multi-leg cluster's probability comes from
  `joint_fn(cluster)` (the engine) and falls back to the Fréchet-bounded independent product
  with a `fallback` flag when the engine returns None. Returns `p`, per-cluster contributions,
  the independent product, the Fréchet bounds of the whole combo, and flags.
- **Combo side semantics.** Pin down, from the Polymarket Combos docs and from our recorded
  `RFQ_REQUEST`/`RFQ_TRADE` data, exactly what the combo's YES position pays (all legs pay?),
  what `side` and `direction` on the RFQ mean for the maker, and what `requested_size` units
  are (`shares` vs notional). Write it in the module docstring with the evidence. The many
  SELL prints at ~0.999 in the trade log are a known puzzle; if you can explain them, do.
- Regression test on `tests/fixtures/rfq_sample_75s.jsonl.gz` (the 75 s live capture of
  2026-10-20, moved into the fixtures so it ships with the repo): class shares must reproduce
  the numbers above within a point (that sample is the ground truth we measured).
- Unit tests for every class, the tennis pattern, the no-game-key refusal, and
  `combo_probability` with and without an engine result.

## Ownership

You own `rfqbot/clusters.py` and `tests/test_clusters.py`. Everything else is as before. Keep
the suite green.
