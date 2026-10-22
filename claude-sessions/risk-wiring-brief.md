# Brief: wire caps, sizing and inventory skew into quote assembly (stage 3, part 3)

Written 2026-10-20 by the planning session (`totalis-e0`). Owner: Ruben. For the NFL session.

## Why

The first exposure run showed why: with every paper quote assumed filled, 2,636 positions
stacked into a 154k-share short on one NE@CHI combo that a single requester re-sent 110 times
in 20 minutes, giving a portfolio 1% expected shortfall of 347% of capital. The per-quote size
cap did nothing. Quote assembly must size every quote against the live book, count pending
quotes as positions until they expire, and lean its margin on concentration.

## Deliverables (your files: `rfqbot/quoting/*`, `rfqbot/risk/*`)

1. **Live book in the quoter.** Maintain a `Book` in the quoting process: add a position at
   quoted size when a quote is emitted (pending), expire it at `submission_deadline + 10 s
   acceptance + 1 s last look + grace` (the listener's expiry rule) unless an execution update
   arrives, in which case it becomes filled at the executed size; settle from
   `market_resolutions` when available. Persist enough to rebuild after a restart
   (`Book.from_decisions` with the real deadlines now logged, or `from_store`).
2. **Sizing.** `size = min(requested, cap_check.allowed_size, kelly_size)` where `kelly_size =
   QUOTE_KELLY_FRACTION x capital x edge / variance` per share with edge = margin earned and
   variance from the combo's payoff (p(1-p) scaled); default fraction 0.25, config. A zero
   allowed size is a skip with reason `cap:<binding_cap>` and the headroom logged.
3. **Inventory skew.** `e_i = QUOTE_K_INV x marginal_risk(book, candidate).d_es1_per_share x
   price_scale`, signed so that a candidate that adds to the book's tail widens the quote and
   one that hedges it tightens (never below the hard invariant of fair). Default `QUOTE_K_INV`
   so that a quote correlated 1.0 with the current worst game adds about one point at the
   outcome cap's midpoint; make it config and log the term.
4. **Repeated-request guard.** Pending exposure already handles the stacking, but also log,
   per combo, how many pending quotes we hold and from how many distinct requesters, and expose
   it in the decision row; quote assembly should not need a special rule once caps see pending
   positions, but we want to see it.
5. **Decisions log**: add allowed_size, binding_cap, headroom, e_i, book summary (positions,
   notional, ES1 fraction) to each row; keep the schema migration in place for old rows.
6. **Run**: 10 minutes of paper quoting with caps live, then `python -m rfqbot risk` on the
   resulting book; report ES1 vs capital, how many quotes were cut or skipped by which cap, the
   e_i distribution, and latency p50/p99 (the check is 14-30 ms, so keep it off the hot path:
   compute headroom per game on the refresh thread and only re-check when a quote would move it).

Report back to `totalis-e0` with those numbers and anything that did not hold.
