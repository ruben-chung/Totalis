# Hand-in summary (bullets for Ruben to write up)

Numbers as of 2026-10-21; sources in `docs/*-notes.md`.

## What was built

### Stage 1: listener and storage

- Websocket listener, verbatim raw log, SQLite store, derived expiry, replay. ~190 RFQ/s recorded
- Feed facts: 0.135% of requests trade (fill rate); SELL fills 3.4x more than BUY; median 133s from first request to print; no requester concentration (top 10 = 7%).

### Stage 2: pricing and correlation (NFL and NBA only)

Each Polymarket leg is matched to its Kalshi market. We calcalate the marginal fairs using a precision weighted price (of Poly and Kalshi). Essentially we track change in variance of both venues (these are both tracking the same true probability) - in any window the change in midprice = true change + venues noise. So comparing the two venues gives each venues noise/variance.

The error on each fair becomes part of the margin: less confident price, wider quote.

Dependence. Legs in different games are independent, so the combo price is just the product. Legs we cannot place in a game are refused. Legs in the same game are priced off the market's own joint distribution of the score: the spread, total and team-total ladders tell us the distribution of each, and fitting them together on a home-away score grid gives the joint. We sample 50k scores from it and read any combo off as the fraction of scores where every leg wins. A calibration layer per market-type pair then checks that against resolved games and shrinks toward independence where the engine overstates.

- Validation on 93 resolved NFL games: Brier 0.130 vs 0.140 for independence; direction right, magnitude ~25% overstated; tails calibrated; 1H coupling overstated. 93 games cannot move most pairs; ~10k would.
- Market clearing vs product of leg mids: BUY 3+ legs +0.5 to +0.75 pt; same-game BUY +2.7 pt; SELL −1.2 pt.
- Round-trip arbitrage: 47 cases in one hour against one maker quoting 22% under fair. Hard rule: never quote below fair on independent combos.

### Stage 3: inventory and risk

Exposure. Every position, filled or still pending, is run through the same 50k scores. For each outcome (team wins, over hits) the delta is how much our P&L moves if it happens vs not; a moneyline share has delta 1 to its own team. From the same scores we get the worst loss per game and the 1% expected shortfall of the whole book. Each is capped (5%, 10%, 15% of $50k). A quote that would breach is cut to what fits or skipped.

Sizing. Min of what was asked, the cap headroom, and a quarter Kelly.

Margin. Fitted per segment (sport, number of legs, direction, same-game or not, how uncertain the fair is). We compare our logged paper quotes to the actual prints to get a fill curve: how often we would have won at each margin. Winner's curse: when we win it is because our price was wrong, so the expected error given a fill is charged. Markout: if the fair keeps moving against us after prints in a segment, that drift is charged too. Wallets: each requester gets a profit score; consistently -EV ones get a premium or are blocked. The margin is the one that maximises fill probability x (margin - curse), subject to a minimum fill rate.

Skew. If a quote adds to our tail risk the margin widens; if it hedges, it tightens toward fair, never through it.

- Before caps, every quote assumed filled: ES1 347% of capital; one requester re-sent one combo 110 times. After caps, 10 min live: ES1 8.4% at end, pinned at 15% when flow repeated; largest stack 7 quotes.
- One-hour run with caps and margin table live: 503,816 RFQs, 947 quoted; ES1 at the 15% cap most of the hour, 4.8% at the end; 1,062 skips by the ES1 cap; margin p50 3.8 pt; 19% of quotes from a fitted node; no errors.
- Latency per quoted RFQ: p50 2.9 ms, p99 20 ms quiet; p99 84 ms under load.
- Margin table, first night: fill probability at zero margin 0.4 to 0.8% per request; optimal margin 0.5 pt on the one populated NBA cell, 2.5 pt pooled; 15 counterfactual wins, so a first reading.
- Markouts: BUY ~0 to +0.4 pt for the maker; SELL −0.2 to −0.6 pt at 15 to 30 min, against −1.2 pt bought below fair. Adverse selection is mechanical (round trips) and on the sell side.
- Wallet block: 22% false positives at 90% confidence under sequential testing; 3.5% at 95%.

### Stage 4: backtest

- Replay harness built: raw log through the live quoting code on a pinned clock, books read as of each RFQ, fills from recorded prints, settlement from resolutions, attribution per margin term, compare across design switches. Reference run in progress.
- Probe RFQs in dry-run build (requester side needs builder credentials).

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
