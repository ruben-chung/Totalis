# Brief: probe RFQs (combo-level markouts and per-RFQ competitor quotes)

Written 2026-10-21 by the planning session (`totalis-e0`). Owner: Ruben. For the NBA session.
Report back to `totalis-e0`. Builds on `docs/margin-optimizer-brief.md` and its notes (markouts on
leg-book fairs, per-segment competitor gap) and `docs/quote-assembly-notes.md` (combo semantics).

## Why

Ruben's idea (2026-10-21). A combo has no continuous market, so every markout we have is the move in
the recombined leg-book fair, and the margin optimiser's competitor gap is a per-segment mean because
the feed shows only the winning print. As a requester we can ask for a quote on the exact combo we
just quoted or filled, read the winning maker price, and never accept. That gives (1) the combo's own
price seconds after our fill, a direct EV measure that includes other makers' view of the dependence,
and (2) the best competing quote per RFQ, which can replace the per-segment delta in the curse term.
Ruben's second idea: disguise the probe by adding a near-certain leg from an independent game, so the
combo has a new condition id and the pattern is not visible as a repeat of the combo we just quoted,
then back the price out by dividing by the extra leg's probability.

Live sending is blocked on credentials (below). Build everything up to the send in dry-run mode, with
the gateway client behind an interface and a fake gateway in tests, so the switch to live is config.

## Verified facts (checked 2026-10-21 against docs.polymarket.com and the repo)

- **The only documented requester path is the Builder Gateway**, for approved builders:
  `POST {gateway}/v1/builder/rfq/requests` creates an RFQ and **returns one quote, the winner of the
  competition**, when it closes (`status: AWAITING_REQUESTER_ACCEPTANCE`, `expires_at` ms, `quote:
  {quote_id, blended_price_e6, maker_amount_e6, taker_amount_e6, total_required_e6, net_receive_e6}`,
  `request: {rfq_id, requestor_public_id, leg_position_ids, condition_id, yes_position_id, ...}`), or
  HTTP 200 with `status: FAILED` and an `error` when no maker quoted. Accept is a separate call we
  never make; letting `expires_at` pass is the normal "no" (the overview quotes a 10 s acceptance
  window; the builders page says to use `expires_at`). Body fields: `signer_address`, `maker_address`
  (the deposit wallet), `signature_type` 3, `leg_position_ids` (2 to 50 underlying market position
  ids, "unique, mutually compatible", not CLOB token ids), `direction` BUY or SELL, `side` YES only,
  `requested_size {unit: notional|shares, value_e6}`. Source: docs.polymarket.com/trading/combos/builders.
- **Auth**: two header sets on the create call. Account headers `POLY_ADDRESS, POLY_API_KEY,
  POLY_PASSPHRASE, POLY_TIMESTAMP, POLY_SIGNATURE` and builder headers `POLY_BUILDER_API_KEY,
  POLY_BUILDER_PASSPHRASE, POLY_BUILDER_TIMESTAMP, POLY_BUILDER_SIGNATURE`; signature is HMAC-SHA256
  with the base64-decoded secret over `timestamp + METHOD + path + body`, base64url with padding. The
  gateway host is given at builder onboarding (builders.polymarket.com); it is not published. What we
  have: `POLYMARKET_API_KEY` and `POLYMARKET_API_SECRET` in `.env`; **missing: passphrase, signer and
  maker addresses, builder registration and credentials, gateway host.** User-only items.
- **Rate limit**: the docs list a 429 "RFQ request rate limit" with no number. Budget conservatively.
- **Our own quoter will see our probes**: every RFQ_REQUEST is public on the maker feed with
  `requestor_public_id`. The quoter must skip our own requester id (config `PROBE_REQUESTER_ID`,
  learned from the first create response's `request.requestor_public_id`); otherwise we quote
  ourselves and the probe measures us.
- **Feed facts** (`docs/quote-assembly-notes.md`, memory): ~190 RFQ/s, 0.135% of requests trade,
  requesters re-poll the same combo at ~1 Hz, so a handful of probes per hour is invisible in volume
  but a probe on the exact combo seconds after a print is a recognisable pattern on the feed.
- **Independence and the disguise leg**: `rfqbot.clusters` certifies that legs in different games
  are independent; `markets` (rfq.db, 441k positions, `outcome_price`) and `data/books.db` give a
  candidate pool of positions priced at or above 0.99 with a live book. Under independence
  `p(XYZ + L) = p(XYZ) p(L)`, so `p_XYZ = quoted / p_L` with relative error equal to the relative
  error of `p_L` (about 1% for a 0.99 leg with a tick-wide book). The legs must be "mutually
  compatible" in the venue's sense (no two positions from the same market, the venue's check); keep
  the extra leg in another sport or another day so the dependence certificate is `disjoint`.
  Caveat (inference): the venue rejects BUY quotes above 0.90909, so for a combo priced near 0.9 the
  disguised combo must stay below that; prefer SELL-direction probes or a 0.97 leg there.
- **Prints to anchor on**: `rfq.db.trades` (rfq_id, combo_id, direction, price_e6, size_e6,
  executed_at_ms) and our decisions in `data/decisions.db` (`rfq_id, p_fair, margin, price_e6,
  requester, condition_id`, stage 3 columns). In paper mode we never fill, so the trigger is "a print
  on a combo we quoted" and, when live, "our fill".
- **Existing pieces to reuse**: `rfqbot/edge/markout.py` (horizons, references, storage pattern),
  `rfqbot/edge/fills.py: competitor_gap`, `rfqbot/marginals.compute_game(prefix, at_ms)` for the leg
  fair at probe time, `rfqbot/catalog.py` for position lookups, `rfqbot/kalshi.py` for an HMAC/RSA
  header pattern, `scripts/run.sh` for a long-running process.

## Deliverables (`rfqbot/probe/`, CLI `python -m rfqbot.probe {run|report|dry-run}`)

1. **Gateway client** `gateway.py`: `BuilderGateway(host, account_creds, builder_creds).create(legs,
   direction, size) -> ProbeResponse(rfq_id, requestor_public_id, condition_id, status,
   blended_price_e6, expires_at, error)`; headers and HMAC per the docs; never calls accept. A
   `DryRunGateway` that logs the exact request body and returns `status: DRY_RUN`; a `FakeGateway`
   for tests that answers from a price function. Config `PROBE_MODE=off|dry|live` (default off),
   `PROBE_GATEWAY_URL`, the credential variables documented in `.env.example` and marked reserved.
2. **Trigger and budget** `scheduler.py`: subscribe to prints and our decisions (poll
   `rfq.db.trades` and the decisions db, as `rfqbot.edge` does); a probe is due when a print lands on
   a combo we quoted (paper) or we are filled (live), after `PROBE_DELAY_SEC` (default 5 s) and again
   at `PROBE_HORIZONS_SEC` (60, 300); budget `PROBE_MAX_PER_HOUR` (default 20), minimum spacing per
   combo, at most `PROBE_MAX_PER_COMBO` (3) per combo per day, priority to combos with the largest
   quoted size and to segments with the fewest probes. Both directions when budget allows (BUY and
   SELL on the same combo give a two-sided mark); record the direction.
3. **Disguise** `disguise.py`: `pick_extra_leg(combo_legs, at_ms) -> ExtraLeg(position_id, p_L,
   book_spread, game_key)` from positions priced at or above `PROBE_DISGUISE_MIN_P` (0.99) with a
   book within one tick, in a different game (certificate `disjoint` via `rfqbot.clusters`), not
   in-play, resolving no earlier than the combo; `back_out(quoted_price, p_L) -> (p_xyz, err)` with
   the error from the extra leg's spread. Config `PROBE_DISGUISE=on|off`; log the extra leg and
   `p_L` with every probe so the undisguised price is recoverable later with a better `p_L`.
4. **Store and analysis** `store.py`, `analysis.py`: `data/probe.db` with one row per probe (trigger
   rfq_id, our decision, our price, direction, legs sent, extra leg, p_L, request body, response,
   blended price, backed-out price, leg-book fair at send time, latency); per probe the combo-level
   markout `sign x (backed-out price - our price)` and its leg-book counterpart at the same instant,
   so the report shows how far the two references disagree and in which segments; per RFQ the
   competitor gap `backed-out price - our fair` next to the segment mean the optimiser uses.
   `report` prints both tables by segment with counts, and a toxicity view per requester of the
   triggering print. Expose `probe_gap(rfq_id)` so the edge pipeline can take the per-RFQ delta
   later (do not change `rfqbot/edge` in this task; message yourself a note in its notes file).
5. **Quoter guard**: a one-line check in quote assembly skipping RFQs whose requester id equals
   `PROBE_REQUESTER_ID` is the NFL session's file; send them the exact line and the config name.
6. **Tests** `tests/test_probe_*.py`: header signing against a fixed vector; the dry-run body matches
   the docs' field set; budget and spacing; disguise picks only disjoint, near-certain, non-in-play
   legs and back-out error propagates; analysis signs (a maker who sold YES and sees the price rise
   has a negative markout); a fake-gateway end-to-end run producing a report.

Out of scope: accepting quotes, signing orders, builder onboarding, any live send until
`PROBE_MODE=live` is set by Ruben with the credentials in place, changes to `rfqbot/edge` or the
quoter beyond the guard line.

## Ownership and coordination

You own `rfqbot/probe/**`, `tests/test_probe_*.py`, `docs/probe-rfq-notes.md`; the planning session
is adding them to `.claude/ownership.json` under `nba`. Do not edit `rfqbot/quoting/**` (NFL session;
send the guard line), `rfqbot/catalog.py`, `rfqbot/store.py`, `rfqbot/__main__.py` (NFL), `.env`
(Ruben; document variables in `.env.example` only, which is shared). Read the live stores with
`mode=ro`; write only `data/probe.db`. In dry-run mode do not run a long process; `dry-run` on the
last hour of prints should produce the bodies it would have sent and the report shape with empty
prices.

## Report back

Send `totalis-e0`: the dry-run output on the last hour (probes due, budget binding, disguise hit
rate: how often a 0.99 leg with a tight book exists in another game), the request body for one
probe, the analysis report on fake-gateway data, test count, and the exact list of credentials and
host values Ruben must supply before `PROBE_MODE=live`. Say plainly what in the docs reading above
is wrong or ambiguous after you have read `docs.polymarket.com/trading/combos/builders` yourself.
Write `docs/probe-rfq-notes.md` with the `/report` skill when done.
