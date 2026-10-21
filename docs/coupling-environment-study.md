# Idea (not developed in this project): environment-stratified realized coupling study

Recorded 2026-10-20 from planning. Ruben's decision: this is a complex problem and will **not**
be developed fully here; the note exists so the idea is on record and can be picked up later.

## The question

The joint engine reads each game's margin-to-total coupling from the market (the spread,
total and team-total ladders pin it). Validating that coupling against realized outcomes on
Polymarket history is underpowered: 93 games cannot confirm or reject a correlation of ~0.1,
whatever the row count. The right test is conditional and needs decades of data.

## The study

1. Data: final scores plus closing spread and total for every NFL game over many seasons
   (public datasets exist; a few thousand games per environment bucket). Halves and quarters
   if available.
2. Stratify by environment: closing spread bucket x closing total bucket (and home/away
   favorite). Within each bucket measure the realized correlation of margin and total, the
   realized joint frequencies of (favorite covers, game goes over) against the product of the
   marginals, and the same for team totals; with proper confidence intervals (one game = one
   observation).
3. Output: a reference table coupling(spread bucket, total bucket) with intervals. Uses:
   (a) compare the engine's ladder-fitted coupling for a live game against the table for its
   environment and flag large departures; (b) a prior for games whose team-total ladders are
   thin or placeholders; (c) the same for the halves coupling, which is where our own prior,
   not the market, currently carries the weight.

## Why it is deferred

The coupling plausibly varies with environment and with era (rule changes, scoring levels),
the half/quarter line history is harder to source, and doing it properly is its own project.
For this bot the market-pinned coupling is kept at face value (calibration lambda = 1.0 unless
game-clustered evidence rejects it), and realized-outcome calibration is used only where it is
powered: structural pairs, tails, and our own fallback priors.
