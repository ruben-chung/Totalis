# Probe RFQs: notes

Written 2026-10-21 by the NBA session (totalis-c8) for the planning session (totalis-e0) and Ruben.
Brief: `claude-sessions/probe-rfq-brief.md`. Code: `rfqbot/probe/` (owner nba). Tests: `tests/test_probe_*.py` (21).
Run: `python -m rfqbot.probe dry-run [--since-hours N] [--show K] [--fairs]`, `report`, `run` (refuses
unless `PROBE_MODE=live` and the credentials are present). State `data/probe.db` (dry-runs write to
`data/probe_dryrun.db` by default so they never consume the live budget). **Nothing has been sent:
`PROBE_MODE` is `off`, no accept call exists anywhere in the package.**

## Shipped

- `rfqbot/probe/config.py`: `ProbeConfig` / `load_probe_config` (env `PROBE_*`, credentials reused from
  `POLYMARKET_*`), `missing_credentials()`, secrets redacted in `to_json()`.
- `rfqbot/probe/auth.py`: `hmac_signature(secret, ts, method, path, body)`, account and builder header
  sets, `headers_for(cfg, ...)`, `redact`.
- `rfqbot/probe/gateway.py`: `ProbeRequest.body()` (the docs' field set, `value_e6` as a string),
  `parse_response`, `HttpGateway` (requester or builder path, refuses to construct without credentials),
  `DryRunGateway` (records url, body, header names), `FakeGateway(price_fn)`; `ProbeResponse.price`.
- `rfqbot/probe/disguise.py`: `candidate_pool(cfg, at_ms)`, `pick_extra_leg(combo, at_ms, pool, salt)`,
  `back_out(quoted, p_L, spread) -> (p_xyz, err)`, `leg_meta`, `parse_ts`.
- `rfqbot/probe/scheduler.py`: `triggers(cfg, since, until)` (prints joined to quoted decisions),
  `plan(triggers, store, cfg, now) -> (plans, PlanStats)`, `build_request`, `execute(plans, gateway, ...)`
  (learns and stores our `requestor_public_id`).
- `rfqbot/probe/store.py`: `ProbeStore` (`probes`, `meta`), budget lookups.
- `rfqbot/probe/analysis.py`: `combo_markout`, `leg_markout`, `gap`, **`probe_gap(rfq_id, db=) -> float |
  None`** for the edge pipeline, `report_lines` (segment table + toxicity table).
- `rfqbot/probe/__main__.py`: `dry-run | run | report`. `.env.example`: the `PROBE_*` block.
- Quoter guard (NFL session, done 2026-10-21): `Assembler.evaluate` returns `skipped:own_probe` when the
  requester equals `QuoteConfig.probe_requester_id` from env `PROBE_REQUESTER_ID`.
- Full suite 433 passed.

## Numbers

Dry-run on the last 3 h of prints (2026-10-21 ~01:40-04:40 UTC), budget as configured (20/h, 3 per combo
per day, 30 s spacing, both directions), every horizon treated as due:

| item | value |
|---|---|
| prints on combos we quoted (triggers) | 26 |
| probe candidates (3 horizons x 2 directions) | 156 |
| selected under budget | 20 (hourly cap binding: 89 dropped by the cap, 47 by spacing) |
| request bodies built, sent | 20, 0 |
| disguise pool (p >= 0.99, spread <= 1 tick, book <= 6 h old, future start) | 109 positions |
| triggers with a disjoint disguise leg available | 24 / 26 (92%) |
| probes disguised | 18 / 20 |

The last hour alone had 9 triggers; the three-hour window is quoted because the per-hour count is
noisy. Spacing binds only because the dry-run evaluates all horizons at one instant; live, the 5 s / 60 s /
300 s probes are 55 s and 240 s apart.

One request body as built (requester gateway, credentials blank because none are configured):

```json
{"url": "https://combos-rfq-gateway-requester-api.polymarket.com/v1/requester/rfq/requests", "method": "POST",
 "body": {"signer_address": "", "maker_address": "", "signature_type": 3,
          "leg_position_ids": ["456516075158246491522113016654928889767739896672108572758668494969453412353",
                               "460457072963582684068553075783773211990898723800314753338243762784160448512",
                               "485492931737693834410980956660716624433243138442593911782744127086190919680",
                               "559532357776497712661851653801619007173109843416708076526357159872309493761",
                               "597875668342969884102353263550819581147354317561175954492868312764946317312",
                               "902035134305283535108089035484857269812454076908791459538017778972963635200",
                               "479905110379680296688569215059614896220729808490419174836387044299930337281"],
          "direction": "BUY", "side": "YES", "requested_size": {"unit": "notional", "value_e6": "20000000"}},
 "headers": ["Content-Type", "POLY_ADDRESS", "POLY_API_KEY", "POLY_PASSPHRASE", "POLY_SIGNATURE", "POLY_TIMESTAMP"]}
```

(the seventh leg is the disguise, a 0.99+ position from another game; `extra_leg_json` and `p_L` are stored
with the row so the undisguised price is recoverable with a better `p_L` later.)

Fake-gateway analysis on the same 26 real triggers (makers simulated as the recombined leg fair at probe
time, 1 pt against the requester, 0.3 pt noise, 13% no-quote): 142 probes, 124 priced, 18 `NO_QUOTES`,
130 disguised, 0 accepted. Report shape (excerpt, pt = probability points):

| segment | h | dir | n | combo markout | leg-book markout | diff | gap vs segment delta |
|---|---|---|---|---|---|---|---|
| nba\|2\|BUY\|disjoint\|s0 | 5 | same | 3 | +0.16 | +1.20 | -1.04 | +0.78 vs +1.06 |
| nba\|3-4\|BUY\|disjoint\|s0 | 5 / 60 / 300 | same | 10 | -0.15 / -0.01 / -0.17 | +0.84 | -1.0 | +0.89 vs +0.49 |
| nba\|3-4\|BUY\|disjoint\|s0 | 5 / 60 / 300 | opp | 10 | +1.83 / +1.73 / +1.92 | +0.84 | +1.0 | - |
| nba\|5+\|BUY\|disjoint\|s0 | 5 | same | 4 | -0.48 | +0.56 | -1.04 | +1.04 vs +0.17 |

The `diff` column is the simulated 1 pt maker markup by construction; with real quotes it is the quantity
of interest (how far the combo market sits from the recombined leg books). The toxicity table lists 14
requesters with 1-5 triggering prints each.

## Corrections to the brief (after reading docs.polymarket.com/trading/combos/{builders,requesters,overview})

1. **The Builder Gateway is not the only requester path.** The requesters page documents a Requester API
   at `https://combos-rfq-gateway-requester-api.polymarket.com`, `POST /v1/requester/rfq/requests`, with
   CLOB L2 account headers only (no builder headers, no onboarding, host published). The builder gateway
   is for builder-attributed orders. The package defaults to the requester path (`PROBE_GATEWAY=requester`);
   `builder` is kept behind the same interface.
2. **Credentials for live are therefore smaller**: `POLYMARKET_API_KEY` / `_SECRET` (have), 
   `POLYMARKET_API_PASSPHRASE`, `POLYMARKET_SIGNER_ADDRESS` and `POLYMARKET_MAKER_ADDRESS` (both the Deposit
   Wallet for `signature_type` 3; Proxy = 1 and Safe = 2 are also accepted, EOA = 0 is rejected), and
   `PROBE_POLY_ADDRESS` = the account signer that owns the API key (defaults to the signer address). Builder
   host + `PROBE_BUILDER_API_KEY/PASSPHRASE/SECRET` only if `PROBE_GATEWAY=builder`.
3. **Rate limit is published for the requester API**: 15 creates per rolling minute per `maker_address`,
   HTTP 429 `RATE_LIMITED`. `PROBE_MAX_PER_HOUR=20` is far inside it.
4. **Acceptance window disagrees across pages**: 5 s on the requesters page, 10 s on the overview, "do not
   assume a fixed window" on the builders page. Irrelevant for us (we never accept), but `expires_at` is
   stored.
5. **The 0.90909 BUY cap is not on these three pages**; it is on the market-makers page (quote assembly
   cites it). Kept as `PROBE_BUY_SAFETY_MAX` for the disguise check.
6. **Timestamp is Unix seconds**, not ms, in `POLY_TIMESTAMP`. Signature recipe otherwise as the brief says
   (HMAC-SHA256, base64-decoded secret, `ts + METHOD + path + body`, base64url with padding).
7. **No idempotency key**: a retried create may open a second RFQ; the loop never retries a create.
8. Ambiguities left: whether makers can see `requestor_public_id` is not stated on these pages (the brief's
   claim comes from our own maker feed, where it is visible, so the guard stands); whether a SELL probe needs
   a share balance at create time (the docs tie balance to accept; untested); whether NO position ids are
   accepted as legs.

## Findings

- `decisions.db` has no requester or condition id column; the trigger joins the print (`rfq.db.trades` ->
  `combos.condition_id`, `requesters.public_id`) to the decision by rfq_id. Prints during a game have no
  leg-book fair (in-play rule), so `leg_fair_send` is NULL for those probes.
- The disguise pool is large (109 tight 0.99+ books with a future start at 04:40 UTC) because every
  "exact score: any other -> No" and heavy-favourite market qualifies; 2 of 26 triggers had no disjoint
  candidate because every pool leg shared a team or game with the combo (`shared_team_diff_game` is not
  `disjoint`).
- `plan` sorts by (probes already in the segment, trigger direction first, quoted size, horizon) and the
  spacing rule exempts the two directions of one (trigger, horizon) pair so a two-sided mark goes out
  together; the per-combo daily cap of 3 means a trigger gets its 5 s pair plus one more probe.
- `execute` records our `requestor_public_id` from the first response (`meta.requester_id`) and logs the
  `PROBE_REQUESTER_ID=` line to set; the quoter guard is live (NFL session) and a no-op while the id is empty.

## Not done / v2 (sent to the planning session)

- Live send: blocked on the credential list above; `run` refuses without them.
- The edge pipeline does not read `probe_gap` yet (brief: no change to `rfqbot/edge` in this task); note
  left in `claude-sessions/margin-optimizer-notes.md`.
- Same-game combos' leg-book fair is the product of leg fairs (no joint engine in the probe analysis).
- SELL-direction probes assume no share balance is needed to create; verify on the first live SELL.
- Requester-id visibility and a possible venue-side pattern check on repeated combos are untested;
  the disguise is on by default for that reason.
