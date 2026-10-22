# Brief: margin optimiser, fill curves, winner's curse, wallet scores (stage 3, part 2)

Written 2026-10-20 by the planning session (`totalis-e0`). Owner: Ruben. For the NBA session.
Report back to `totalis-e0`.

## Why

Quote assembly charges a flat base edge (0.005) plus uncertainty terms with unit multipliers.
Ruben's framing, agreed in planning: an RFQ is a first-price auction among makers, so we are
filled disproportionately when our fair is too low (the winner's curse on our own estimation
error), and the margin we can charge is an optimisation against our fill rate. Both the fill
curve and the curse are estimable from what we already log. On top sit a measured, direction-
asymmetric informational premium (markouts: buy flow ~0, sell flow -0.25 pt at 15 min, -0.57 pt
at 30 min today) and per-wallet scores with a block rule for wallets we are consistently -EV
against. Fill rates and adverse selection differ by RFQ type, so everything is estimated per
segment with hierarchical pooling.

## Data (all exist)

- `data/decisions.db` (quote assembly, NFL session): one row per RFQ with p_fair, u_m, u_d, the
  quoted price and size, decision, per-leg marginals, cluster info, requester, direction, sport,
  n_legs, timestamps. Today it only holds two 10-minute paper runs; step 0 below makes it
  continuous.
- `data/rfq.db`: `rfqs`, `trades` (rfq_id, requester_id, direction, side, price_e6, size_e6,
  executed_at_ms), `requesters` (public_id), `combos`/`combo_legs`.
- `data/books.db` and `data/kalshi_books.db`: point-in-time leg quotes (`rfqbot.ladders.
  game_markets`, `rfqbot.kalshi_books.kalshi_for_legs`); `rfqbot.marginals.compute_game`
  gives a fair per leg at any instant.
- Combo semantics: `rfqbot/quoting/semantics.py` (BUY = maker sells YES; notional is a USD
  budget incl. fees; fills print shares x price / notional ~0.965).

## Step 0: continuous paper quoting

Start one continuous paper-quoting listener alongside the recording listener, on its own
database so the two never share a writer:
`RFQ_DB_PATH=data/rfq_quote.db RAW_LOG_DIR=none QUOTE_DECISIONS_DB=data/decisions.db
scripts/run.sh rfqbot listen --quote` (check `rfqbot listen --help` for the exact flags; the NFL
session ran it this way for its 10-minute tests). Confirm it is writing decisions before fitting
anything; a few hours of decisions are needed for the segment tables.

## Deliverables (`rfqbot/edge/`, you own it; tests `tests/test_edge_*.py`)

1. **Counterfactual fills** `fills.py`: join each quoted decision to the trade print on the same
   rfq_id. The print is the best competing quote, so at markup m over our p_fair we would have
   won iff (BUY) `p_fair + m <= trade_price` or (SELL) `p_fair - m >= trade_price`, size
   permitting (our size vs the requested/printed size). Produce, per decision, the would-win
   flag at the actual quote and on a grid of markups.
2. **Fill curves** `fillcurve.py`: F(m) per segment, where a segment is (sport, n_legs bucket
   {2, 3-4, 5+}, direction, same_game | disjoint, sigma bucket of u_m + u_d), fitted as a
   monotone decreasing curve in m with hierarchical pooling: a cell borrows from its parent
   (drop sigma bucket, then same_game, then n_legs) until it has at least N_min decisions.
   Store the table with counts and the pooling level used.
3. **Winner's curse** `curse.py`: among would-have-won decisions, the gap between the later fair
   (`compute_game` at +15 and +30 min, with Kalshi as a cross-check where matched) and our
   p_fair at quote time, as a function of how far we undercut the print, per segment. Report it
   as a measured conditional error alongside the model form: with our error sd sigma = u_m + u_d
   and a competitor gap delta, E[error | win] = -sigma * phi(z)/Phi(z), z = (delta - m)/sigma.
   Fit delta per segment from the fill curve; compare the model curse to the measured one.
4. **Markout monitor** `markout.py`: continuous table of maker markouts by horizon (1, 5, 15,
   30 min), direction, segment, and requester, against Polymarket fair and against Kalshi where
   matched (the Kalshi comparison tells us whether a zero Poly markout is "no information" or
   "nobody updated the book"). Store in `data/edge.db`, refresh on a timer.
5. **Wallet scores** `wallets.py`: per requester public_id, the running mean and variance of our
   realised profit per fill (markup earned minus 30-min markout), shrunk toward the population
   by fill count; a sequential block rule: block when the upper 90% confidence bound of expected
   profit per fill is below zero after at least `EDGE_BLOCK_MIN_FILLS` (default 8) fills; blocks
   expire after `EDGE_BLOCK_DAYS` (default 7) unless re-triggered; a manual allow/deny list.
   Below the block threshold the shrunk score becomes a per-wallet premium (`EDGE_WALLET_K`
   times the negative expected profit). Round-trip detection (same wallet, same combo, opposite
   direction within 10 s; already tagged by quote assembly) is an immediate block regardless.
6. **Optimiser** `optimize.py`: per segment, m* = argmax F(m) * (m + E[error | win at m]) subject
   to F(m) >= `EDGE_MIN_FILL_RATE` (default 0, i.e. unconstrained; the policy knob), plus the
   directional informational premium = max(0, -mean markout at 30 min for that direction and
   segment). Output `rfqbot/edge/margin_table.json`: per segment, m*, F(m*), expected profit
   per request and per fill, the pooling level, and counts. Quote assembly will load this in
   place of QUOTE_BASE_EDGE (the NFL session wires it; you deliver the table and a loader
   `margin_for(segment) -> MarginTerms(base, curse_mult, info_premium, wallet_premium, blocked)`).
7. CLI `python -m rfqbot.edge {fit|report|wallets}` and a short `claude-sessions/margin-optimizer-notes.md`
   with the first tables once a few hours of decisions exist.
8. Tests: counterfactual fill logic for both directions and sizes; monotone fill-curve fit and
   pooling fallbacks; the curse formula against a simulated auction (known sigma, known
   competitor distribution); the sequential block rule's false-positive rate on simulated
   neutral wallets (should be low) and detection on a -2 pt wallet; the optimiser on a synthetic
   fill curve where the answer is known.

## Ownership

You own `rfqbot/edge/*`, `tests/test_edge_*.py`, `claude-sessions/margin-optimizer-notes.md` (added to
`.claude/ownership.json` under nba). Quoting integration is the NFL session's; send it the loader
signature when stable. Suite is 316 green.

## Report back

Decisions accumulated, the first fill-curve table with counts and pooling levels, the measured
vs model curse per segment, markouts by direction against both references, the wallets that
would currently be blocked (with n), the m* table, and anything in this brief that did not hold.
