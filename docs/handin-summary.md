# Hand-in summary (bullets for Ruben to write up)

Numbers as of 2026-10-21; sources in `docs/*-notes.md`.

## What was built
- High level: Calculate Fair value of each marginal, then estimate dependence to find the joint. Quote this FV + margin. 

### Stage 1: listener and storage

- Websocket listener, verbatim raw log, SQLite store, derived expiry, replay. ~190 RFQ/s recorded
- Feed facts: 0.135% of requests trade (fill rate); SELL fills 3.4x more than BUY; median 133s from first request to print; no requester concentration (top 10 = 7%).


### Stage 2: pricing and correlation (NFL and NBA only)

Each Polymarket leg is matched to its Kalshi market. We calcalate the marginal fairs using a precision weighted price (of Poly and Kalshi). Essentially we track change in variance of both venues (these are both tracking the same true probability) - in any window the change in midprice = true change + venues noise. So comparing the two venues gives each venues noise/variance.

The error on each fair becomes part of the margin: less confident price, wider quote.

Dependence: Legs in different games are independent, so the combo price is just the product (with different markets like Politics, Economics etc these assumptions cannot be made). Legs in the same game are priced off the market's own implied joint distribution of the score (spread, total ladders). Fitting all of these at once into a score grid gives us the joint distribution. 

Comparing our dependence model against resolves games (by combo type: spread x total, ML x spread, etc..) we apply shrinkage towards independence if our dependence is statistically overstated.

We sample 50k scores from the score grid and read any combo off as the fraction of scores where every leg wins. 

- Validation on 93 resolved NFL games: Brier 0.130 vs 0.140 for independence; direction right, magnitude ~25% overstated; tails calibrated; 1H coupling overstated. 

### Stage 3: inventory and risk

Exposure: Every position, filled/pending, is run through the same 50k scores (above). For each outcome/game-state (team wins, over hits) we define Delta "how much our P&L moves if it happens vs not". A ML has delta 1 to its own team winning. From the same scores we can calculate drawdown and risk metrics. 

Sizing: Min(Volume Asked, Exposure Budget Remaining, Quarter Kelly)

Margin. We want the margin that maximises expected profit per RFQ = P(fill at margin m) x EV(fill at m). The optimzer picks m to maximise this. 
P(fill) is the fill curve: we compare our logged paper quotes to the actual prints and count how often we would have won at each margin. 

EV per fill is margin minus two costs. (1) Winner's curse: we get filled more precisely when our price is wrong. So conditional on a fill our fair is wrong against us, by more when it is less certain. The cost is that expected error given a fill. (2) Price Drift (Adverse Selection): Implied Fair price moving away from us after fill. 
So EV = Margin - curse - drift.

Wallets: on top, each requester gets a profit score from our fills against them. Consistently -EV ones get a premium, or are blocked.

Skew. If a quote is +corr to our exposure the margin widens; and vice versa. This skew cannot make us cross FV though. 


### Stage 4: evaluation

Validation: The dependence engine is scored on resolved games. On 93 NFL games Brier is 0.130 vs 0.140 for independence. Direction right, magnitude ~25% overstated, tails fine, 1H coupling overstated. 93 games is too few to move most pairs; ~10k would.

Paper runs: The bot quotes the live feed and logs every decision. Before caps, with every quote assumed filled, ES1 was 347% of capital (one requester re-sent one combo 110 times). With caps, ES1 sits at the 15% cap when flow repeats and 4.8-8.4% otherwise; largest stack 7 quotes. One hour with caps + margin table live: 504k RFQs, 947 quoted, 1,062 skipped by the ES1 cap, margin p50 3.8 pt, no errors. Latency per quoted RFQ p50 3 ms, p99 20 ms (84 ms under load).

What the prints say: 0.135% of requests trade; SELL fills 3.4x BUY. Market clears at product + 0.5 to 0.75 pt on 3+ leg combos, +2.7 pt on same-game, -1.2 pt on SELLs. Markouts after prints: BUY ~0 to +0.4 pt for the maker, SELL -0.2 to -0.6 pt, so EV on SELL flow is roughly 1.2 - 0.6 = +0.6 pt. Round trips: 47 in one hour against one maker quoting 22% under fair; hence never quote below fair on independent combos.

Margin table, first night: P(fill) at zero margin is 0.4 to 0.8% per request; optimal margin 0.5 pt on the one populated NBA cell, 2.5 pt pooled; 15 counterfactual wins, so a first reading. Wallet block rule: 22% false positives at 90% confidence (sequential test), 3.5% at 95%.

Backtest: Replay the raw log through the live quoting code on a pinned clock, books read as of each RFQ, fills from recorded prints, settlement from resolutions, P&L attributed per margin term. Then the same window under each switch: joint vs independence, caps on/off, fixed edge vs margin table. Harness built; reference run in progress. Realized P&L needs resolutions over the week.

Probe RFQs (dry-run build): request our own quote on a combo we just filled, read the price, never accept. Gives the combo's price seconds after our fill and the best competing quote per RFQ. Needs builder credentials to go live.

- 378 tests. Built with one planning session plus peer Claude Code sessions; briefs and notes under `docs/`.

## Design decisions worth stating

- Dependence from the market's own joint (ladders to IPF to scenarios), not from a sports model. Validated, calibrated, honest about sample size.
- Independence is certified structurally (different game) before any product pricing; anything uncertifiable is refused.
- Confidence on every fair becomes margin; margin terms are logged per RFQ, so attribution is possible.
- Caps are on instantaneous exposure including pending quotes, not on quote count.
- Realized-outcome evidence counted in games, standard errors clustered by game.

## Limits

- Paper only: no credentials, no fills, no execution updates; fill path tested by unit tests and counterfactual prints.
- Four hours of recorded flow; 26 of 1,385 traded combos fully resolved on day one (65 partly; 31 have a leg missing from the catalog).
- NFL-only joint engine; NBA same-game (38% of quotable flow) skipped; props fit exists but not wired; 96% of RFQs are sports without ladders.
- Venue weight measured pre-game only; Kalshi leads price discovery in both sports; in play not covered.

## Next, in priority order

1. Backtest compare table: joint vs independence, caps on/off, fixed edge vs table.
2. Realized P&L as resolutions arrive over the week.
3. NBA same-game engine.
4. Probe RFQs live once credentials exist; per-RFQ competitor gap into the optimiser.
5. Hedging with singles; strategic feedback damping.
6. Full list: `docs/future-work.md`.
