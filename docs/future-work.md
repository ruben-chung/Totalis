# Future work: what we would do with more time and resources

This is a take-home project for Totalis. The bot as delivered covers stages 1 and 2 of the spec
(listener and storage; pricing with dependence, validated and running in paper mode); stages 3
and 4 (inventory and risk, backtest) are designed below but not built. This document records
every extension we identified along the way, why it matters, and what it would take. Maintained by the planning session; sessions send additions by message.
Items marked **(idea only)** were explicitly scoped out by decision, not forgotten.

## Ruben's ideas

Ideas Ruben raised in the planning conversations, in the order they came up, with where each one
stands. The rest of this document is the engineering backlog they generated.

- **Aggregate Kalshi and Polymarket marginals by liquidity, flag wide spreads and venue disagreement**
  (2026-10-20). Built: `rfqbot/marginals`, precision-weighted with a Kalshi prior of 2.0 on NFL/NBA.
- **Carry a confidence level on every fair and let it widen the margin at execution** (2026-10-20).
  Built: the `u_m` term in quote assembly is the marginal's expected error.
- **Certify independence first, because then a combo is just a product** (2026-10-20). Built:
  `rfqbot/clusters` classifier; different game means independent for sports; politics vs economics
  deliberately not certified.
- **Model dependence within a game through the market-implied joint distribution of points**
  (2026-10-20). Built for NFL: `rfqbot/pricing`; soccer, NBA same-game and player props deferred.
- **Measure calibration per market-type pair, never pooled** (2026-10-20). Built: per family-pair
  calibration with game-clustered errors.
- **Realized-outcome tests are underpowered; count evidence in games and match environments by
  spread and total** (2026-10-20). Recorded: `docs/coupling-environment-study.md` (idea only).
- **Exposure caps per outcome through an options-like delta, a sizing function, and margin as a
  function of adverse selection** (2026-10-20). Built: `rfqbot/risk` deltas and caps, Kelly sizing,
  inventory skew in quote assembly.
- **Winner's curse: when our price is wrong we always get filled; margin is an optimisation under a
  fill-rate constraint** (2026-10-20). Built: `rfqbot/edge` fill curves, curse term, optimiser.
- **Track opposing wallets and block consistently negative-EV counterparties; measure fill rate and
  adverse selection per RFQ type** (2026-10-20). Built: wallet scores and blocks, per-segment tables.
- **Measure EV as the price move x seconds after a fill** (2026-10-21). Built as the markout monitor
  in `rfqbot/edge/markout.py` on leg-book fairs; combo-level version below.
- **Probe RFQs: request a quote on the combo we just filled, read the makers' prices, never accept**
  (2026-10-21). Being built: `rfqbot/probe` (brief `docs/probe-rfq-brief.md`); live sending waits
  on requester credentials.
- **Disguise the probe by adding a near-certain leg from another game, then back the combo price
  out** (2026-10-21). Part of the probe brief: a 0.99 leg from an independent game changes the
  combo's condition id, so makers see a new combo; price of XYZ = quoted price / p(extra leg) under
  independence, with a relative error equal to the extra leg's own uncertainty (about 1%).

## Pricing: marginals

- **Venue weights from data, not priors.** Kalshi's precision multiplier (2.0 on NFL/NBA) is a
  placeholder. Measure it per sport with a lead-lag test (which venue's moves predict the
  other's) and a Brier test against resolutions, from the two book stores we already record.
- **Disagreement resolved by precision, not by gap.** When venues disagree the layer takes
  Polymarket; when the disagreement is caused by a garbage Polymarket book the better answer is
  Kalshi. Compare precisions.
- **Third reference source.** De-vigged Pinnacle (or a consensus odds feed) would dominate both
  venues on majors and settle disagreements. Paid, adds a dependency.
- **Shifted-line translation.** A Kalshi line at 7.5 when Polymarket lists 6.5 is informative
  through the game model; today it is only a flag.
- **Fee modelling.** Kalshi taker fees (~0.07 p(1-p)) and Polymarket fee schedules are not in
  the marginal; they matter for any real cross-venue hedge.
- **Calibrated expected error.** Bucket legs by predicted error and check realized error against
  it in the backtest, so "confidence" is a forecast of our own mistakes, not a formula.

## Pricing: dependence

- **Environment-stratified coupling study (idea only).** Decades of NFL scores with closing
  lines, coupling by spread/total bucket, as a reference and a prior for thin ladders. See
  `docs/coupling-environment-study.md`.
- **Quarters.** The engine supports FG/1H/2H; quarter ladders exist on Polymarket and quarter
  markets appear in flow (quarter spreads/totals, highest scoring quarter).
- **Regulation vs overtime.** Full-game ladders include OT; quarter and highest-scoring-quarter
  markets exclude it. Needs an explicit regulation margin with a tie mass and an OT model.
- **Live second-half coupling.** The 2H table is gated on consistency and almost never passes on
  historical placeholders; judge it on live Sunday-slate books where 2H depth is real.
- **In-play pricing.** Condition the scenario set on the current score and clock. Everything is
  pre-game today; in-play same-game pairs are skipped.
- **NBA same-game model.** The engine is NFL-only; NBA needs its own scale parameters and
  ladder depth is thin at season start. Same machinery.
- **Soccer.** The bulk of same-game flow in the sample was soccer. The exact-score grid is the
  market's own joint distribution; a Dixon-Coles Poisson smooths illiquid cells and splits
  halves; corners need their own count model. Needs a soccer leg parser (per-game team codes,
  no global table) first.
- **Other sports with ladders.** CFB (deep ladders already), NHL, tennis (doubles slug
  pattern), esports, UFC (one leg per event; nothing to model).
- **Player props: pair-specific effects (idea only).** A passer-scorer duo or minutes trade-offs
  are pair-level residual terms; today residual correlations are by position pair. The box-score
  dataset already supports fitting them for frequently paired players.
- **Player props: full engine extension.** Share-plus-residual columns on the scenario set
  (fit parameters exist in `rfqbot/props/props_fit.json`); NFL prop families (receiving yards, TDs) are
  parsed but not modelled; a 2024-25 NBA season would double the fit sample.
- **Exclusive-group and bracket logic beyond games.** Futures and tournament markets (team wins
  the semi and the final) are linked without sharing a game; today any leg without a game key
  is refused.
- **Non-sport dependence.** Politics and economics pairs (Iran markets appeared in flow) need
  explicit rules or models; refused today by design.
- **Empirical-Bayes prior variance.** The per-pair shrinkage uses the group's estimation
  variance as the prior variance; it should be the between-pair heterogeneity, which cannot be
  estimated from 93 games. A precise pair (2H spread with FG spread, 1.05 ± 0.22) therefore still
  lands near the group. Cluster-bootstrap the intervals by game as a cross-check once games grow.
- **Validation on untraded ladder lines.** The validation sample is the ~130 traded legs per game
  with snapshots; untraded tokens sit at 0.5 placeholders. A price-history pull for them would
  extend the ladders used in validation.
- **NFL player props pricing source.** Receiving yards, touchdown scorers and the rest are
  classified and skipped; quoting them needs a props pricing source and the share-model
  extension.
- **Multi-season calibration.** Per-pair calibration is limited to 93 NFL games; Kalshi's
  settled history and more seasons of Polymarket would make the per-pair factors informative.

## Quoting and execution

- **Fill model.** The decisions log plus RFQ_TRADE prints give, per price-vs-fair and size, the
  probability of being the winning quote. Replace the fixed base edge with a fill-probability
  maximising margin.
- **Adverse-selection model.** Round-trip detection is a tag today; make it a requester score
  and widen or refuse for requesters who repeatedly harvest.
- **Requester profiling.** 2,000+ requesters with no concentration; cluster by behaviour
  (poll rate, combo structure, direction) and size quotes accordingly.
- **Size as a function of confidence** beyond the tier table; and quoting both directions.
- **Per-RFQ competitor gap.** The margin optimiser's delta (gap between the print and our fair) is a
  per-segment mean because the feed shows only the winning print, never the losing quotes. A live
  best-competing-quote estimate, and a two-sided curse that includes our size relative to the print,
  would make the curse term per RFQ.
- **Always-valid block rule.** The wallet block is a fixed-level bound re-tested after every fill
  (~30 looks), so its false-positive rate at 90% confidence is 22% on neutral wallets (3.5% at 95%).
  Replace it with a mixture confidence sequence so the error rate holds under continuous monitoring.
- **Joint-engine later fair in the markout monitor.** Markouts for NFL same-game combos use the product of
  leg fairs as the later reference; the joint engine should price them.
- **Probe RFQs for combo-level markouts and competitor quotes.** Markouts today are the move in the
  leg-book fair after a print, because a combo has no continuous market. As a requester we can send
  our own RFQ on the exact combo we just quoted or filled, read the makers' quotes through the
  authenticated requester channel, and never accept: that gives the combo's own price seconds after
  our fill (a direct EV measure that includes the other makers' view of the dependence) and the best
  competing quote per RFQ, which the margin optimiser currently replaces with a per-segment mean.
  Needs the requester credentials (passphrase missing today), a budget of probes per hour so the
  pattern does not read as spam, both directions per probe, and a check that the probe itself does
  not move makers (requests are public on the feed). Live only; it cannot be backtested.
- **Fill curves from real fills.** The counterfactual win rule (we win iff our margin is at or below the
  print's gap) ignores size priority and quotes that arrived after the print; a live maker's own fills
  would replace it. Tables need ~40 quoted-sport prints per hour to fill; SELL and NFL nodes had none
  on the first night and fall back to the default edge.
- **Real submission.** Signing, maker onboarding with Polymarket, passphrase and addresses;
  the paper path is complete up to the signed frame.
- **Hedging.** Lay off leg exposure on the CLOB or on Kalshi when a combo fills; requires the
  fee model and cross-venue settlement-rule mapping, which the matcher already provides.

## Inventory and risk (stage 3, built)

- Exposure deltas, caps and marginal risk (`rfqbot/risk`, `docs/risk-exposure-notes.md`) are wired
  into quote assembly with Kelly sizing and the inventory skew (`docs/risk-wiring-notes.md`).
- **Fill path unexercised live.** The feed is unauthenticated, so no execution updates arrive;
  trade-size fills and settlement in the live Inventory are covered by unit tests only. With real
  fills the book stays full and the caps bind for the day; in paper mode pending quotes expire 31 s
  after their deadline and the same combo is re-quoted every half minute.
- **Cross-sport combos** use the first leg's sport for the sport-notional cap.
- **Margin table loader** (`rfqbot.edge.margin_for`) replaces the fixed base edge in quote assembly:
  being wired by the NFL session.
- **Settlement by condition id.** `Book.settle()` needs condition ids, so it only works for books
  loaded with the catalog; decisions-log books cannot settle yet.
- **Tail attribution for cross-game combos** is first order (exact for single-game positions).
- **Repeated RFQs are the dominant inventory risk**: one requester re-sent the same combo 110
  times in 20 minutes; a per-quote size cap is no protection, only the outcome and game caps
  are. Pending quotes must count as positions until expiry.

## Backtest (stage 4)

- Replay the raw log through the handler with point-in-time books from both venues; fills from
  the recorded trades; realized P&L from `market_resolutions`; sensitivity to the calibration
  factors and the shock set; attribution by margin term.

## Data and operations

- **Process supervision.** Listener, book recorder, Kalshi feed and history jobs run as separate
  processes; a supervisor with restart and a single health command.
- **Storage.** Raw logs run at ~3 GB/day for RFQs and ~0.5 GB/day for Kalshi; retention and
  compaction policy.
- **Slug date convention drift.** Polymarket's slug dates switched from UTC to ET mid-season
  for the NBA; the parsers tolerate either when a kickoff is known, but a monitor for the NFL
  flipping is cheap insurance.
- **Catalog gaps.** WNBA (same grammar, own team table), Kalshi first-basket grammar,
  All-Star and exotic Finals families, Kalshi `KXNFLHIGHSCOREQ` ticker grammar.
- **Rescheduled games and exotic families.** 717 NBA history legs stay unparsed by agreement:
  Finals exotic markets, All-Star codes, and two real reschedules whose slug date matches neither
  the ET nor the UTC tip date. A v2 keys reschedules on Gamma `gameStartTime` instead of the slug.
- **Player identity collisions.** The player key is initial plus surname on both venues, so two
  players sharing it in one game (Jalen and Jaylin Williams) are refused rather than guessed.
  Disambiguate with Kalshi team plus jersey number and Polymarket's full name.
- **Resolution tracking at scale.** Resolve, prices, pairs and reparse are one-shot CLI runs;
  nothing keeps `market_resolutions` current unless resolve is scheduled. Voids and 50-50
  resolutions need their own P&L handling.
- **Historical books, not just prices.** `hist_prices` holds the last-trade price only, so the
  engine's thin-book term is never exercised on history; pull CLOB book snapshots or Kalshi
  quotes at the same pre-kickoff offsets.
- **Pair cap.** `hist_pairs` caps at 60k pairs per game and the cap binds on 88 of 93 NFL games,
  dropping prop-with-prop pairs beyond the rank order; raise the cap or sample instead of truncate.
- **History depth.** Pulls are series-based (nfl-2026, nba-2026) and start this season; earlier
  seasons, and every other sport in the RFQ feed, have no history or ladders at all.
- **Full depth offline.** The book recorder keeps coalesced top-of-book (one row per token per
  second, raw deltas off); depth within 0.05 of the touch is the only depth signal kept, so a
  full book cannot be reconstructed after the fact.
- **Unmappable tokens.** A position Gamma cannot map to a CLOB token is retried every 30 min but
  never gets a book or a place in a ladder.
- **Gamma contract tests.** Its quirks (closed=true for resolved markets, newest-first paging,
  NFL props in side events but NBA props inside main events) are handled empirically; a contract
  test against the live API would catch the next change.
