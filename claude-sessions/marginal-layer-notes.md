# Marginal layer: what was built and what the first real run says (2026-10-20)

Built against `claude-sessions/marginal-layer-brief.md`. Package `rfqbot/marginals/` (config, model, sources,
__main__), tests `tests/test_marginals_model.py` + `tests/test_marginals_cli.py` (36 tests; suite 276 green).

```bash
.venv/bin/python -m rfqbot.marginals --game nfl-ne-chi-2026-10-23                 # latest quotes, wall clock
.venv/bin/python -m rfqbot.marginals --game nfl-ne-chi-2026-10-23 --at-ms T       # point in time: no quote after T, staleness vs T
.venv/bin/python -m rfqbot.marginals --game ... --kalshi synthetic                # fabricated exact Kalshi books (until kalshi_books lands)
.venv/bin/python -m rfqbot.marginals --game ... --log [--log-db data/marginals.db] # append every marginal + features
.venv/bin/python -m rfqbot.marginals --game ... --tier wide --limit 20
```

Library: `rfqbot.marginals.compute_game(prefix, at_ms, cfg=, books_db=, rfq_db=, kalshi_mode=, kalshi_db=,
now_ms=)` -> `[LegMarginal(quote, match, kalshi, marginal)]` (`poly=` / `kalshi=` override the readers);
`marginal_for_leg(mq, match, kq, sport=, now_ms=, cfg=, poly_confirmed_ms=, kalshi_confirmed_ms=)` for
one leg; `combine(...)` on already-shaped `VenueQuote`s. The Kalshi producer is duck-typed
(`confidence`, `kalshi_side`, `yes_bid`, `yes_ask`, sizes, depths, `at_ms`), and `--kalshi auto` reads
`rfqbot.kalshi_books.kalshi_for_legs` over `data/kalshi_books.db` (landed 2026-10-20 evening).

**Staleness on change-only stores.** Both `book_top` and `kalshi_top` write a row only when the top
changes, so a row's age measures quietness, not staleness. The model therefore measures staleness
from the last *confirmation*: the feed's newest row of any instrument at or before the evaluation
instant (`sources.poly_confirmations`, which also requires the token to be subscribed then, and
`sources.kalshi_confirmation`). The raw row age is kept as `VenueEstimate.age_s` and logged. Without
this, 258 of 622 NE@CHI legs were tier `stale` on quiet books an hour before anything moved.

## Formula (fractions 0..1; 0.01 = one probability point)

* `p_v` = size-weighted microprice at the touch `(bid*ask_size + ask*bid_size) / (bid_size + ask_size)`,
  plain mid when a size is missing. Kalshi side `no`: `bid = 1 - yes_ask`, `ask = 1 - yes_bid`,
  sizes and depths swap sides, so `p_no = 1 - p_yes` exactly.
* `sigma_v = hs_v * (1 + c_thin / (1 + depth_v / D0)) + c_stale * staleness_v`,
  `depth_v = min(bid_depth, ask_depth)` (sizes at best as fallback), `staleness_v` = seconds since
  the quote was last confirmed (above), `c_stale` switches from pregame to in-play once
  `game_start_time <= now`.
* `precision_v = 1 / sigma_v^2`; Kalshi x `KALSHI_PRIOR[sport]` (nfl 2.0; nba and else 1.0, see claude-sessions/venue-weight-notes.md) and
  x 0.25 for `rules_differ`. `shifted_line` / `none` never combine.
* `p = sum(prec * p_v) / sum(prec)`, `err = 1 / sqrt(sum(prec))`, floored at 0.005 (0.02 when
  degenerate), capped at 0.5 (an absolute probability error above that carries no information).
* Disagreement `|p_poly - p_kalshi| > hs_poly + hs_kalshi`: no averaging, `p = p_poly`,
  `err >= gap`, tier `disagree`; gap > 0.10: tier `skip` (p still reported).

Defaults (`rfqbot/marginals/config.py`, env `MARGINAL_<KEY>`; `MARGINAL_KALSHI_PRIOR=nfl=2,nba=1`):
c_thin 1.0, D0 500 sh, D_min 50 sh, c_stale 3e-6/s pregame (1 pt/h) and 1e-3/s in play (1 pt/10 s),
stale warn/drop 600/3600 s pregame and 15/60 s in play, inplay_fresh 15 s, wide when spread >
0.2 p(1-p) + 0.02, one_sided_sigma 0.15, gap_skip 0.10. All placeholders for the backtest.

## Flags -> actions

| flag | effect |
|---|---|
| `wide:<v>`, `thin:<v>` | note; tier `wide` / `thin` when the venue is used |
| `stale:<v>` | note past warn; venue weight 0 past drop; tier `stale` when a used venue is stale |
| `inplay` | in-play staleness scale and gates; tier at least `stale` if newest used quote >= 15 s old |
| `one_sided:<v>` | venue skipped when the other is two-sided; alone: present side, sigma 0.15, tier `wide` |
| `rules_differ` | Kalshi precision x 0.25 |
| `kalshi_unused:<conf \| no_quote>` | Poly only |
| `disagree`, `disagree_hard` | see formula; tier `disagree` / `skip` |
| `degenerate` | p outside [0.015, 0.985]; err floor 0.02 |
| `no_quote[:<v>]` | nothing usable: p None, tier `skip` (per-venue variant is a note) |

Tier priority: skip > disagree > stale > wide > thin > ok, set only by venues whose weight is at
least `tier_w_min` (0.05): a wide Poly book next to a tight Kalshi book keeps `wide:poly` as a note
but the tier describes the estimate actually used. Fees: both venues fee-exclusive, no adjustment
(quote assembly owns fees).

## First real run: NE@CHI 2026-10-23 (only NFL game with ladders), 2026-10-20 evening

622 legs in 0.16 s, Kalshi unused everywhere (`kalshi_books` not landed): ok 146, wide 454,
skip 22 (no book row yet). Moneyline 0.397 / 0.603 ± 0.005 on 173k shares; 1H moneyline ± 0.011.
The wide gate fires on nearly every ladder strike: Poly quotes deep 1H/FG lines with half
spreads of 0.12 to 0.34 (e.g. 1H spread -13.5: 0.111 ± 0.50, book 96 shares). That is the data,
not the formula: those legs need the joint engine's ladder fit, not their own book. Point in
time at 10 minutes into recording: 363 legs skip (no row yet), as expected. `--kalshi synthetic`
combines on 596 legs with w_kalshi ~0.66 (prior 2.0 on a matching spread, half the depth).

## With real Kalshi quotes (same game, an hour later)

Kalshi combines on 295 of 622 legs (exact 3,700 / rules_differ 144 NFL rows in `leg_matches`;
the rest shifted_line / none). ok 289, wide 296, disagree 6, skip 27, thin 4; Kalshi weight ~0.69 on
the moneyline (prior 2.0, 620k contracts vs 173k shares). The disagree rule finds real things:

* 1H and 1Q moneylines (`rules_differ`): Poly 0.410 vs Kalshi 0.372, Poly 0.590 vs 0.540; the gap is
  Kalshi's three-way tie rule. Reported as Poly-only with err = gap (0.038 / 0.050), tier `disagree`;
  the 1Q gap of 0.108 exceeds `gap_skip` -> `skip`.
* Full-game moneyline Bears: Poly 0.603 vs Kalshi 0.592 on two 0.005-half-spread books -> `disagree`
  with err 0.011. The threshold (sum of half spreads) is tight for tight books; a floor on it
  (e.g. + 0.005) is a calibration question, left as is.
* 1Q spread -6.5: Poly book 0.18/1.00 (hs 0.41), Kalshi 0.164 ± 0.01. The rule takes Poly (0.591
  ± 0.445, `skip`). That is what the brief says, but when the disagreement is driven by a garbage
  Poly book the better answer is Kalshi; worth revisiting (compare precisions, not only gaps).

## Interpretations where the brief was open

* `degenerate` fires on the combined p outside [0.015, 0.985] regardless of one-sidedness (the
  brief tied it to a one-sided book; a two-sided 0.99/1.00 book is just as degenerate).
* An `exact` / `rules_differ` match with no Kalshi quote rows is flagged `kalshi_unused:no_quote`
  (the brief only names `shifted_line` / `none`).
* Both venues one-sided: Poly's present side wins. Poly dropped (stale) with Kalshi fresh: Kalshi
  alone with weight 1.0 (allowed by the precision-weighted mean).
* err is capped at 0.5 (not in the brief) so a 0.65-wide book does not report err 0.6 on p 0.11.
* Kalshi prices are probabilities 0..1 like Poly's (confirmed by the Kalshi feed brief).
* Staleness is measured from the feed's last confirmation, not the row (change-only stores, above).

## Not done

Calibration of any constant; fee handling; Kalshi real data (adapter and tests only); no
edits outside `rfqbot/marginals/*`, `tests/test_marginals_*`, this note.

## Venue prior revised (2026-10-21)

The Kalshi prior default is now `nfl 2.0, nba 1.0` (was 2.0 for both). The NFL session measured the venue weight on 4.4 h of
matched pre-game books (`claude-sessions/venue-weight-notes.md`): tight-quote precision ratio var_poly / var_kalshi is 0.90 for NBA
(interval 0.72 to 1.13, so no evidence Kalshi deserves extra weight) and 2.2 for NFL (1.5 to 3.7). The env override is unchanged.
Backtest runs started before this change used 2.0 for NBA; the run configs store the marginal config only through this file,
so compare runs made on either side of the change are not comparable.

## Review fixes (2026-10-22)

- **Locked / crossed book** (ask <= bid) is unusable: flag `crossed:<venue>`, no estimate. Reproduced before the fix: Poly 0.40/0.40 beside Kalshi 0.45/0.46 gave p = 0.40 with w_poly = 1 (sigma floored at 1e-4, precision 1e8). Half spreads are now floored at half a tick (`hs_floor`: Poly 0.0005, Kalshi 0.005).
- `kalshi_prior` values and `kalshi_prior_default` must be > 0 (`MarginalConfig` raises; nba=0 used to be a ZeroDivisionError with Kalshi alone).
- **Disagreement** is tested as gap > `disagree_k` (2.0) x sqrt(sigma_p^2 + sigma_k^2) instead of the sum of half spreads. On disagreement p stays the precision-weighted value (it used to fall back to Polymarket alone), err = max(err, gap), tier `disagree`; beyond `gap_skip` tier `skip`. k = 2 is looser than the old rule (a thin book has sigma up to 2x its half spread); k is a placeholder for the backtest.
- **rules_differ** is a bias, not noise: `sigma_k^2 += rules_differ_bias^2` (0.02, placeholder) instead of a flat x0.25 precision. A prop (slug with points / rebounds / assists / threes / steals / blocks) with differing rules is not combined (`kalshi_unused:props_rules`).
- **Not done (4, the payoff conversion):** converting a Kalshi period winner to Polymarket's tie convention (p_team + 0.5 p_tie from the TIE strike) needs the Kalshi tie market's book, which this layer is not handed. The bias term stands in for it; its size is a guess (a half-weighted 1H tie is about 0.01-0.02 in NFL), to be set from the backtest.
- **Microprice** shift is shrunk by depth / (depth + `micro_d0`, 500).
- **err independence:** `1/sqrt(sum precision)` assumes independent venue errors. It is floored at `err_rho` (0.5) x the smallest sigma of the venues used.
- **Kalshi confirmation** in live mode is per ticker (`ticker_seen.last_seen_ms`, `kalshi_ticker_confirmations`), falling back to the feed heartbeat; a replay cannot use it (the table is current state). Polymarket confirmation per connection is the recorder's (not changed here).
- Regression tests for all of these in `tests/test_marginals_model.py` and `tests/test_marginals_cli.py`.
