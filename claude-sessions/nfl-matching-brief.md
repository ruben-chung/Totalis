# Brief: deterministic NFL market identity + Kalshi matching for rfqbot

Written 2026-10-20 by the planning session (totalis-94) for the session building this.
Owner: Ruben. When done, send a summary back to session `totalis-94` via SendMessage.

## Goal

Build the first piece of stage 2 of the Polymarket Combos RFQ bot in `/Users/rubenchung/Totalis`:
a **deterministic** identity layer for NFL markets that

1. parses every NFL market in our Polymarket catalog into a structured `LegSpec`
   (game, family, period, team, line, side, settlement notes),
2. parses Kalshi NFL game markets into the same structure,
3. matches Polymarket legs to Kalshi markets with an explicit confidence tag
   (`exact` / `shifted_line` / `none`) and a list of settlement-rule differences,
4. reports coverage so we know exactly what fraction of NFL flow we can classify.

NFL only, on purpose: 32 teams, regular slugs, so everything can be table-driven.
No fuzzy matching anywhere. Unknown input raises or returns an explicit parse error,
never a guess. This layer is the foundation for the dependence model (same-game detection)
and for stage 3 exposure netting, so **game identity must be first class**.

Do not build pricing, book fetching, or other sports. Keep `LegSpec` sport-agnostic so
soccer can be added later. Do not re-run the full catalog sync (7 min, 215k markets);
`data/rfq.db` already has 24,956 NFL position rows (12,478 markets).

## Repo facts

- Python 3.11, package `rfqbot/`, run with `.venv/bin/python`, tests `.venv/bin/python -m pytest -q`.
  `README.md` describes stage 1 (listener/storage). Conventions: dataclasses, type hints,
  `httpx`, `logging`, pytest-asyncio. Keep existing tests green.
- `markets` table (SQLite, `data/rfq.db`): `position_id, market_id, condition_id, slug, title,
  outcome, outcome_index, outcome_price, volume, tags (json), synced_at_ms`. One row per outcome
  position (index 0 and 1). NFL rows have `tags` containing `"nfl"`. `outcome_price` is a stale
  placeholder, ignore it.
- Entry points live in `rfqbot/__main__.py` (subcommands `listen`, `stats`, `catalog`, `replay`).
  Add new subcommands there. Schema lives in `rfqbot/store.py`.

## Polymarket NFL facts (verified against the catalog and the Gamma API on 2026-10-20)

Slug grammar: `nfl-{away}-{home}-{YYYY-MM-DD}[-{family}][-{period}][-{team}][-{line}pt5]`

- Second code is **home** (verified: `nfl-ind-min-...-spread-home-7pt5` has title
  "Spread: Vikings (-7.5)"; Gamma says Vikings home, line -7.5).
- The slug date is the **UTC date of kickoff**, not the local game date. Monday night
  Bears at Seahawks, Nov 2 ET 8:15pm, is slug `nfl-chi-sea-2026-11-03` with
  `gameStartTime 2026-11-03 01:15:00+00`. Kalshi uses the **ET date** (`26NOV02CHISEA`).
  This off-by-one hits every night game. See "Game identity" below.
- 32 slug codes: ari atl bal buf car chi cin cle dal den det gb hou ind jax kc la lac lv
  mia min ne no nyg nyj phi pit sea sf tb ten was. (`la` = Rams, `lac` = Chargers.)
- Bare game slug (`nfl-ind-jax-2026-11-01`, title "Colts vs. Jaguars") is the moneyline.
  Verify its outcomes and tie handling from the Gamma description.

Family inventory (markets, index-0 rows), slug family token -> count:

```
team-total-{team} 946 | total 903 | spread-home 731 | spread-away 731
1h-/2h- team-total-{team} 688 each | 1h-/2h- spread-home/away 688 each
1q..4q spread-home/away 387 each | 1q..4q total 258 each | 1h-/2h- total 215 each
team-off-yards-{team} 31 | team-tds-{team} 28 | two-pt-conversions 15 | safety 14
highest-scoring-quarter-{q1,q2,q3,q4,other} 14 each (neg-risk group, Yes/No)
exact-margin-{tie, short-1, short-7, short-14, short-21-plus, long-1, long-7, long-13, long-19, long-25-plus} 14 each
1q..4q-moneyline, 1h-/2h-moneyline 14 each | 1q..4q-btts-points 14 each
```

Title grammar per family (team = nickname only, e.g. "Vikings", "49ers"):

```
Spread: Vikings (-7.5)                    outcomes [Vikings, Colts]   (favorite first)
1H Spread: 49ers (-8.5) / 1Q Spread: ...  same, with period prefix
Broncos vs. Cardinals: O/U 62.5           outcomes [Over, Under]
Bengals vs. Ravens: 1Q O/U 10.5           period inside the title
Colts Team Total: O/U 12.5                outcomes [Over, Under]
Raiders 2H Team Total: O/U 17.5
Browns vs. Titans: Highest Scoring Quarter - Q2   outcomes [Yes, No]
Colts vs. Jaguars                         moneyline
```

Semantics verified from Gamma descriptions:
- Full-game spread "Vikings (-7.5)": resolves Vikings if they win by 8 or more; **overtime
  included**; postponed stays open; cancelled resolves 50-50.
- Full-game total: Over if combined >= 63 for line 62.5.
- Highest scoring quarter: regulation only, **OT excluded**; ties -> "Other".
- Quarter / half markets: read each family's description for OT handling and record it;
  do not assume.

Gamma enrichment (public, no auth), use it to *validate* the slug parser, not to replace it:
- `GET https://gamma-api.polymarket.com/markets?slug={slug}` -> `description`,
  `sportsMarketType` (spreads | totals | moneyline | highest_scoring_quarter | ...),
  `line` (signed float; determine the sign convention for `spread-away` vs `spread-home`
  from both the `line` field and the description, and document it), `gameStartTime`,
  `outcomes`, `clobTokenIds`, `positionIds`, `events[0].slug` (the game event),
  `bestBid`, `bestAsk`, `spread`, `rfqEnabled`.
- `GET https://gamma-api.polymarket.com/events?slug=nfl-chi-sea-2026-11-03` -> all 280
  markets of one game in one call. Good for building fixtures.

## Kalshi NFL facts (verified against the public API on 2026-10-20)

Base `https://api.elections.kalshi.com/trade-api/v2`, no auth needed for reads:
- `GET /markets?series_ticker={S}&status=open&limit={n}` with `cursor` pagination.
- `GET /events/{event_ticker}`; `GET /series?category=Sports` (4,021 series, 388 with NFL
  in the ticker; `tags=Football` narrows to 599). Enumerate and classify the game-level ones.

Ticker grammar: `{SERIES}-{YY}{MON}{DD}{AWAY}{HOME}-{SUFFIX}`, date in **ET**, team codes
uppercase and **not guaranteed equal to Poly's** (verify all 32 from live tickers, e.g. the
Rams and Chargers). Team order appears to be away-home ("Dallas vs Philadelphia", DALPHI),
verify rather than trust.

Game series seen live, with example and strike fields:

```
KXNFLGAME-26NOV02CHISEA-SEA          "Seattle wins"                         winner, suffix = team
KXNFLSPREAD-26OCT26DALPHI-PHI8       "Philadelphia wins by over 7.5 points?" floor_strike 7.5, strike_type greater
KXNFLTOTAL-26OCT26DALPHI-63          "Full Game: over 62.5 points scored?"   floor_strike 62.5
KXNFLTEAMTOTAL-26OCT26DALPHI-PHI43   "Philadelphia over 42.5 points scored"  floor_strike 42.5
KXNFL1HSPREAD / KXNFL1HTOTAL / KXNFL1HTEAMTOTAL / KXNFL2HSPREAD / KXNFL2HTOTAL
KXNFL1QSPREAD / KXNFL1QTOTAL / KXNFL2QTOTAL   (check 3Q/4Q and 2H team totals: KXNFL2HTEAMTOTAL returned nothing)
KXNFL1H-26OCT25DENARI-TIE            "Will neither team win the 1st Half?"  (3-way half result)
KXNFL1HWINNER, KXNFL1HFT, KXNFL2Q, KXNFL4Q (period winners), KXNFL1QBTTS, KXNFLEQBTTS,
KXNFLHIGHSCOREQ, KXNFLOT, KXNFLSAFETY and KXNFLSFTY (both exist), KXNFL2PTCONV,
KXNFLGAMEFG, KXNFLTEAMFG, KXNFLNOSCOREQ, KXNFLRACE, KXNFLCOMEBACK, KXNFLTD, KXNFLANYTD
```

Note the suffix integer is `ceil(line)` (7.5 -> 8, 62.5 -> 63); the authoritative line is
`floor_strike`. Kalshi spreads are **one-sided per team**: `PHI8` YES = "Philadelphia wins by
more than 7.5". So Poly "Spread: Eagles (-7.5)" outcome Eagles == Kalshi PHI8 YES, and the
Cowboys outcome == PHI8 NO. A `DAL8` market is a *different* contract, not the complement.
Kalshi rules text is in `rules_primary`; store it.

## Deliverables

1. `rfqbot/sports/nfl_teams.py`: the 32-team table. Canonical code, city, nickname, full name,
   Poly slug code, Kalshi ticker code, Kalshi city string as used in titles ("Philadelphia"),
   plus any aliases you actually observe. Exact lookups only; unknown raises.

2. `rfqbot/sports/legspec.py`: sport-agnostic `LegSpec` dataclass. Suggested fields:
   `venue, sport, game_key, away, home, kickoff_utc (optional), family, period
   (FG|1H|2H|1Q|2Q|3Q|4Q), team (canonical or None), line (float or None), side
   (team code for spread/moneyline, over|under, yes|no), includes_ot (True|False|None),
   venue_ids (position_id / clob token / Kalshi ticker), rules_text (optional)`.
   Plus `canonical_key(spec)` (venue-independent contract identity, e.g.
   `nfl|2026-10-25|IND@MIN|FG|spread|MIN|-7.5|cover`) and `complement(spec)` where defined.

3. `rfqbot/sports/nfl_poly.py`: `parse_poly_nfl(slug, title, outcome, outcome_index)`.
   Pure, offline, works from catalog fields alone so replay and backtest never need the network.
   Cross-check title against slug (team nickname vs slug code, period, line) and **flag any
   disagreement as a parse error** rather than picking one.

4. `rfqbot/sports/nfl_kalshi.py`: `parse_kalshi_nfl(ticker, title, floor_strike, strike_type,
   rules_primary, expected_expiration_time)`. Same output type.

5. `rfqbot/kalshi.py`: thin public-API client + `kalshi_markets` table (ticker, event_ticker,
   series_ticker, title, yes_sub_title, floor_strike, cap_strike, strike_type, rules_primary,
   expected_expiration_time, close_time, status, synced_at_ms, parsed fields). Command
   `python -m rfqbot kalshi-sync --sport nfl`. Only the game-level NFL series. Be gentle with
   rate limits.

6. `rfqbot/sports/matcher.py`: given a Poly `LegSpec`, return `MatchResult(confidence,
   kalshi_ticker, kalshi_side, notes)` where confidence is `exact` (same canonical key and
   compatible rules), `shifted_line` (same game/family/period/team, nearest Kalshi lines above
   and below), or `none`. `notes` lists every settlement difference found (OT inclusion,
   tie/push handling, cancellation 50-50 vs void). Never collapse a rule difference into `exact`.

7. Command `python -m rfqbot nfl-coverage`: over all NFL rows in `markets`: percent parsed by
   family and period; every unparsed slug/title (target: zero, or each one explained); match
   confidence counts by family; list of Poly families with no Kalshi counterpart; all distinct
   settlement-rule notes. With `--gamma` it enriches a sample via the Gamma API and reports any
   field where the slug-derived parse disagrees with Gamma (`line`, `sportsMarketType`,
   `gameStartTime`, outcomes).

8. Tests: fixtures of real rows (one per family and period, both outcomes; ~20 real Kalshi
   tickers across series); round trips; the CHI-SEA UTC-vs-ET case; `complement`; the
   `spread-away` sign convention; and a test that parses every NFL row in `data/rfq.db` when
   the file exists (skip otherwise).

## Game identity

Use the pair (away, home) plus the kickoff date, and treat Poly's slug date as UTC and
Kalshi's as ET. Recommended: the canonical `game_key` carries the **ET date**, derived from
Gamma `gameStartTime` when available; when only the slug is available, match games by team
pair with a ±1 day tolerance and assert uniqueness (no NFL team plays twice within three days).
Document whichever rule you settle on in the module docstring.

## Report back

Send `totalis-94` a short summary: coverage numbers by family, families with no Kalshi
counterpart, the settlement-rule differences you found, and surprises (sign conventions,
Kalshi series naming inconsistencies, both-side spreads, anything in the data that contradicts
this brief). If something in this brief turns out to be wrong, say so plainly.
