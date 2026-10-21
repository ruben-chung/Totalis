# Future work

Take-home project for Totalis. Stages 1 to 3 built, stage 4 in progress. Everything identified
and not built, by area. **(idea only)** = scoped out by decision.

## Ruben's ideas

In order raised, with status.

- Aggregate Kalshi and Polymarket marginals by liquidity; flag wide spreads and disagreement. Built: `rfqbot/marginals`.
- Confidence on every fair that widens the margin. Built: `u_m` term.
- Certify independence first; then a combo is a product. Built: `rfqbot/clusters`; politics vs economics never certified.
- Dependence within a game from the market-implied joint score distribution. Built for NFL: `rfqbot/pricing`.
- Calibration per market-type pair, never pooled. Built.
- Realized-outcome tests are underpowered; count evidence in games; match environments by spread and total. Recorded: `docs/coupling-environment-study.md` (idea only).
- Exposure caps per outcome via an options-like delta; a sizing function; margin as a function of adverse selection. Built: `rfqbot/risk`, Kelly sizing, inventory skew.
- Winner's curse: wrong prices always fill; margin is an optimisation under a fill-rate constraint. Built: `rfqbot/edge`.
- Track opposing wallets, block consistently negative-EV ones; measure fills and adverse selection per RFQ type. Built.
- EV as the price move x seconds after a fill. Built: `rfqbot/edge/markout.py` on leg-book fairs.
- Probe RFQs: request a quote on the combo we just filled, never accept. Being built: `rfqbot/probe`, dry-run until credentials.
- Disguise the probe with a near-certain leg from another game; back out p = quoted / p_leg. In the probe brief.
- Hedge combo exposure with singles. Below.
- Strategic feedback between our margin function and other makers. Below.

## Pricing: marginals

- Venue weights: measured 2026-10-21 on 4.4 h pre-game (`docs/venue-weight-notes.md`): NBA tight-quote precision ratio 0.90 [0.72, 1.13] so prior 1.0; NFL 2.2 [1.5, 3.7] so keep 2.0. Kalshi leads in both sports (Polymarket closes 4-6% of a gap per minute, Kalshi under 1%), which argues for a lead/staleness term rather than a bigger multiplier. Not covered: in play, the last hour before kickoff, news bursts.
- Resolve venue disagreement by precision, not by taking Polymarket.
- Third reference source (de-vigged Pinnacle or consensus odds).
- Translate shifted Kalshi lines through the game model instead of flagging them.
- Fee modelling on both venues.
- Calibrate the expected-error forecast against realized error.

## Pricing: dependence

- Environment-stratified coupling study (idea only): `docs/coupling-environment-study.md`.
- Quarters; regulation vs overtime (quarter markets exclude OT).
- Live second-half coupling; judge the 2H table on Sunday-slate books.
- In-play pricing: condition scenarios on score and clock.
- NBA same-game model (same machinery, own scale parameters).
- Soccer (exact-score grid is the market's joint; Dixon-Coles for thin cells); CFB, NHL, tennis, esports.
- Player props: pair-specific effects (idea only); full share-plus-residual engine extension (fit exists in `rfqbot/props/props_fit.json`); NFL prop families and a pricing source.
- Exclusive-group and bracket logic beyond games; non-sport dependence rules.
- Empirical-Bayes prior variance from between-pair heterogeneity; cluster-bootstrap intervals.
- Validation on untraded ladder lines; multi-season calibration.

## Quoting and execution

- Per-RFQ competitor gap and a two-sided curse that includes our size; today delta is a per-segment mean because losing quotes are unobserved.
- Always-valid confidence sequence for the wallet block; the fixed-level rule re-tested after every fill has a 22% false-positive rate at 90% (3.5% at 95%).
- Joint-engine later fair for same-game markouts.
- **Strategic feedback with other makers.** F(m) is fitted from prints, which are other makers' quotes; each RFQ is a first-price sealed-bid auction. If we widen, the prints we lose to widen, the refit widens us again, and their optimisers do the same: a positive feedback loop toward wider margins (or a race to fair). Damp refits (cap the change in m*, hold out RFQs at the old margin as a control, watch print-minus-fair drift over days), and model the auction itself: competitors per segment and their cost distribution, solve for the equilibrium bid. Open question; nothing built.
- **Probe RFQs.** Combo-level markouts and best competing quote from our own requests on the combo we just quoted, read and never accepted; disguise with a 0.99 leg from an independent game. Requester API (CLOB L2 headers, 15 creates per minute per address, 5 s acceptance window); needs the API passphrase and the deposit-wallet addresses, no builder onboarding. Built dry-run only (`rfqbot/probe`, 21 tests): on the last 3 h of prints, 26 triggers, 20 bodies built under a 20/h budget, a disjoint 0.99 disguise leg existed for 24 of 26. Left: wire `probe_gap(rfq_id)` into the curse term as the per-RFQ delta; joint-engine later fair for same-game probes; confirm on the first live SELL probe whether a share balance is required at create; venue-side detection of repeated combos is untested, so disguise stays on.
- Fill curves from real fills; the counterfactual rule ignores size priority and later quotes. Curse is unmeasurable so far (13 counterfactual wins in 6 h, single prints at -16 to -24 pt dominate); switch the optimiser to the measured curse once a node has a few hundred wins. The two widest pooled nodes still price at 4.5-10 pt and only serve unseen cells.
- Nodes without prints (75-85% of quotes on night one) keep the stage 2 uncertainty charge; the fitted formula applies only where a fill curve exists.
- Requester profiling by behaviour; size as a function of confidence; quoting both directions.
- Real submission: signing, maker onboarding, passphrase, addresses.
- **Last look.** Makers with about $2,500 of combo notional volume can apply (request form, established contact) for a confirmation step: after a quote is selected the ws sends RFQ_CONFIRMATION_REQUEST and the maker answers CONFIRM or DECLINE before `confirm_by` (1 s in the overview; the timeout outcome is undocumented). Makers declining more than 15% of selected quotes over a one-hour lookback may be paused for a few minutes. It is the standard adverse-selection defence: re-price the combo at selection time and decline if the fair moved against us by more than the margin. Unavailable in paper mode and until the volume threshold; the decline rule is a one-line check on the live fair once the path exists.
- **Adverse-selection fixes, cheap set.** Tilt the fair toward Kalshi when venues disagree (it leads); refuse legs inside ~10 min of tip-off until there is an in-play fair; seed the SELL information premium at ~1.5 pt instead of waiting for 30 markouts; split SELL flow into decided-legs exits (no premium) and live-legs sells (premium).
- **Hedging with singles.** Outcome deltas in `rfqbot/risk/exposure.py` are the per-leg hedge ratios; a single-leg hedge removes first-order exposure and leaves the dependence residual. Hedge when spread plus fees is below the drop in marginal ES1. Needs the fee model, `rules_differ` handling and real fills.

## Inventory and risk

- Fill path is unexercised live: unauthenticated feed, no execution updates; pending quotes expire and the same combo is re-quoted every half minute, so caps bound instantaneous, not cumulative, exposure.
- Cross-sport combos use the first leg's sport for the sport cap.
- `Book.settle()` needs condition ids; decisions-log books cannot settle.
- Tail attribution for cross-game combos is first order.
- Quoted-RFQ latency (p99 53 ms with the edge monitor and backtest running) depends on machine load; dedicated core or lower monitor cadence before live.

## Backtest

- Walk-forward refits of the margin table inside the replay.
- A cost for displacing the printed maker (round-trip harvesters would hit us instead).
- Realized P&L needs resolutions refreshed as games settle: 447 of 1,835 distinct traded combos fully resolved after the second resolve pass (26 after the first), 274 partly, 38 with a leg missing from the catalog. Schedule `rfqbot.history resolve` hourly (under 2 min a pass) and `pull --since yesterday` daily.

## Data and operations

- Process supervisor with restart and one health command. On 2026-10-21 the machine lost network for 5.6 h (15:45 to 21:25 UTC); the recorders reconnected on their own but nothing alerted, so the day's flow has a gap. A gap monitor on the raw log (alert when no frame for 60 s) is the minimum.
- Retention: ~3 GB/day RFQ raw, ~0.5 GB/day Kalshi.
- Monitor slug-date convention drift (NBA switched UTC to ET mid-season).
- Catalog gaps: WNBA, Kalshi first-basket and `KXNFLHIGHSCOREQ` grammars, All-Star and Finals families; 717 NBA history legs unparsed; reschedules keyed on Gamma `gameStartTime`.
- Player identity collisions (initial plus surname): disambiguate with team, jersey, full name.
- Scheduled resolution tracking; voids and 50-50 P&L.
- Historical books, not last-trade prices; full depth offline (recorder keeps coalesced top of book).
- `hist_pairs` cap of 60k per game binds on 88 of 93 NFL games.
- History depth: this season only, NFL and NBA only.
- Unmappable tokens never get a book; Gamma contract tests.
- NFL player-prop legs are not parsed in history (49,268 of 108,580 NFL hist_legs carry a parse error, nearly all prop side events), so NFL hist_pairs cover game markets only.
