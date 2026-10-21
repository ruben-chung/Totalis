# Hand-in summary (bullets for Ruben to write up)

Numbers as of 2026-10-21; sources in `docs/*-notes.md`.

## What was built

### Stage 1: listener and storage

- Websocket listener, verbatim raw log, SQLite store, derived expiry, replay. ~190 RFQ/s recorded
- Feed facts: 0.135% of requests trade (fill rate); SELL fills 3.4x more than BUY; median 133s from first request to print; no requester concentration (top 10 = 7%).

### Stage 2: pricing and correlation (NFL and NBA only)

Each Polymarket leg is matched to its Kalshi market (exact, rules differ, shifted line, or none) so both venues' books can price it. The fair for a leg is a precision-weighted average of the Polymarket and Kalshi books, where each venue's precision is one over an error variance built from half-spread, depth and staleness, and Kalshi carries a measured multiplier (NFL 2.0, NBA 1.0, from the variance of mid changes on 4.4 h of matched books). The expected error of that fair is carried forward as a margin term. Legs are then classified for dependence: same market, same exclusive group, same game, different game, or no game key. Different-game legs are certified independent and priced as a product; no-game-key legs are refused. Same-game NFL legs are priced from the market's own joint distribution of points: the spread, total and team-total ladders are fitted monotonically, combined by iterative proportional fitting on a home-away score grid (halves coupled the same way), and sampled into 50k scenarios on which every market is an indicator, giving the combo probability, a shock interval, and Fréchet bounds. A calibration layer per market-family pair, with game-clustered errors, shrinks the engine toward independence where evidence rejects it.

- Validation on 93 resolved NFL games: Brier 0.130 vs 0.140 for independence; direction right, magnitude ~25% overstated; tails calibrated; 1H coupling overstated. 93 games cannot move most pairs; ~10k would.
- Market clearing vs product of leg mids: BUY 3+ legs +0.5 to +0.75 pt; same-game BUY +2.7 pt; SELL −1.2 pt.
- Round-trip arbitrage: 47 cases in one hour against one maker quoting 22% under fair. Hard rule: never quote below fair on independent combos.

### Stage 3: inventory and risk

Every position, filled or pending, is mapped onto the same scenario set: for each outcome the delta is the expected payoff given the outcome minus given its complement, so a moneyline share has delta one to its own win and a prop a positive fraction. Cross-game positions factorise; unmodeled legs enter as Bernoulli factors. From the scenario P&L we take the per-outcome exposure, the per-game worst loss and the portfolio 1% expected shortfall, and cap each (5%, 10% and 15% of $50k); a quote that would breach is cut to the headroom or skipped, and pending quotes count until they expire. Size is the minimum of the request, the cap headroom and a quarter-Kelly bound using margin net of the inventory charge over p(1−p). The margin itself is a fitted table per segment (sport, legs, direction, structure, uncertainty bucket): a fill curve from counterfactual wins against recorded prints, a winner's-curse term equal to the expected error conditional on winning, an information premium from the markout of the fair after prints, and a per-wallet premium or block from a shrunk profit score; the optimiser picks the margin maximising fill probability times margin net of curse, subject to a fill-rate floor. An inventory skew, the ES1 slope of the candidate times outcome-cap usage, widens quotes that add to the tail and tightens hedges toward fair, never through it.

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
