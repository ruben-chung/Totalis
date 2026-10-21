# Hand-in summary (bullets for Ruben to write up)

Numbers as of 2026-10-21; sources in `docs/*-notes.md`.

## What was built

- Stage 1: ws listener, verbatim raw log, SQLite store, derived expiry, replay. ~190 RFQ/s recorded since 2026-10-20 21:27 UTC.
- Stage 2: NFL/NBA leg parsers (100% of catalog rows parse); Kalshi matching (exact / rules_differ / shifted_line / none); Polymarket ladder recorder; Kalshi feed; precision-weighted marginals; independence classifier; NFL joint score engine; per-pair calibration; quote assembly in paper mode.
- Stage 3: outcome deltas and ES1 on the scenario set; hard caps (outcome 5%, game 10%, ES1 15% of $50k); Kelly sizing; inventory skew; fill curves, winner's curse, markouts, wallet scores; fitted margin table wired into quoting.
- Stage 4: replay harness in progress; probe RFQs in dry-run build.
- 378 tests. Built with one planning session plus peer Claude Code sessions; briefs and notes under `docs/`.

## Headline numbers

- Flow: 0.135% of requests trade; SELL fills 3.4x BUY; median 133 s from first request to print; no requester concentration (top 10 = 7%).
- Round-trip arbitrage observed: 47 cases in one hour against one maker quoting 22% under fair. Hard rule: never quote below fair on independent combos.
- Market clearing vs product of leg mids: BUY 3+ legs +0.5 to +0.75 pt; same-game BUY +2.7 pt; SELL −1.2 pt.
- Markouts (maker P&L after print): BUY ~0 to +0.4 pt; SELL −0.2 to −0.6 pt at 15 to 30 min. Adverse selection is mechanical (round trips) and on the sell side.
- Joint engine on 93 resolved NFL games: Brier 0.130 vs 0.140 independence; direction right, magnitude ~25% overstated; tails calibrated; 1H coupling overstated.
- Calibration per family pair, game-clustered; market-pinned couplings held at 1 unless rejected. 93 games is not enough to move most pairs; ~10k would be.
- Exposure before caps, every quote assumed filled: ES1 347% of capital; one requester re-sent one combo 110 times. After caps, live 10 min: ES1 8.4% at end, pinned at 15% whenever flow repeated; largest stack 7 quotes.
- Quoting latency with caps live: p50 2.9 ms, p99 20 ms per quoted RFQ.
- Margin table, first night: fill probability at zero margin 0.4 to 0.8% per request; optimal margin 0.5 pt on the one well-populated NBA cell, 2.5 pt pooled; 15 counterfactual wins, so first reading only.
- Wallet block rule: 22% false positives at 90% confidence under sequential testing; 3.5% at 95%.

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
- Venue weight 2.0 is a prior (measurement running).

## Next, in priority order

1. Backtest compare table: joint vs independence, caps on/off, fixed edge vs table.
2. Realized P&L as resolutions arrive over the week.
3. NBA same-game engine.
4. Probe RFQs live once credentials exist; per-RFQ competitor gap into the optimiser.
5. Hedging with singles; strategic feedback damping.
6. Full list: `docs/future-work.md`.
