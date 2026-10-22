# Brief: deterministic NBA market identity + Kalshi matching for rfqbot

Written 2026-10-20 by the planning session (totalis-94) for the session building this.
Owner: Ruben. When done, send a summary back to session `totalis-94` via SendMessage.
Companion to `claude-briefs/nfl-matching-brief.md`, which a parallel session (`totalis-47`) is building
right now in this same directory. Read that brief first: it defines the shared `LegSpec`,
the matcher contract, the Kalshi client and the coverage report. This brief adds the NBA.

## Goal

Same shape as the NFL task, for the NBA: parse every NBA market in our Polymarket catalog into
a structured `LegSpec` (game, family, period, team or player, line, side, settlement notes),
parse Kalshi NBA game markets into the same structure, match Polymarket legs to Kalshi with an
explicit confidence tag (`exact` / `shifted_line` / `none`) plus settlement-rule notes, and
report coverage. 30 teams, regular slugs, table-driven. No fuzzy matching. Unknown input raises
or returns an explicit parse error, never a guess.

Out of scope: pricing, order books, WNBA (152 rows, probably the same grammar with a `wnba-`
prefix; keep the team table extensible), other sports.

## Working alongside the NFL session (important: same directory, no git)

This repo is not a git repository, so two sessions editing the same file will clobber each other.

Files owned by `totalis-47` (the NFL session). Do **not** create or edit them:
`rfqbot/sports/legspec.py`, `rfqbot/sports/matcher.py`, `rfqbot/kalshi.py`,
`rfqbot/__main__.py`, `rfqbot/store.py`, and their tests.

Files you own: `rfqbot/sports/nba_teams.py`, `rfqbot/sports/nba_poly.py`,
`rfqbot/sports/nba_kalshi.py`, `rfqbot/sports/nba_players.py`, `rfqbot/sports/nba_coverage.py`
(standalone, run as `python -m rfqbot.sports.nba_coverage`, so `__main__.py` stays untouched),
`tests/test_nba_*.py`, `tests/fixtures/nba_*`, `claude-briefs/nba-matching-notes.md`.

Coordination: message `totalis-47` when you start. Ask it to expose a per-sport registry
(sport -> Polymarket parser, Kalshi parser, Kalshi series list, slug-date convention) so you can
register the NBA without editing its files. If `legspec.py` does not exist yet, write your
parsers against the field list in the NFL brief and import it lazily; do not write your own copy.
If `kalshi.py` does not exist yet, capture Kalshi fixtures with curl into `tests/fixtures/nba_*`
and have the coverage script's `--kalshi` mode use httpx inline; switch to the shared client
once it lands. If you need a change in a shared file, ask `totalis-47` for it.

## Repo facts

Python 3.11, package `rfqbot/`, run with `.venv/bin/python`, tests `.venv/bin/python -m pytest -q`.
`markets` table in `data/rfq.db`: `position_id, market_id, condition_id, slug, title, outcome,
outcome_index, outcome_price, volume, tags (json), synced_at_ms`, one row per outcome position.
NBA rows have `tags` containing `"nba"`: 1,478 rows (739 markets) over 52 games, Oct 20 to 26.
`outcome_price` is a stale placeholder, ignore it. Do not re-run the catalog sync (7 min).

## Polymarket NBA facts (verified against the catalog and Gamma on 2026-10-20)

Slug grammar: `nba-{away}-{home}-{YYYY-MM-DD}[-{family}][-{player-slug}][-{line}pt5]`

- Second code is **home** (verified: `nba-atl-orl-...-spread-home-2pt5` is "Spread: Magic (-2.5)",
  Magic home; `nba-dal-hou-...-1h-spread-home-5pt5` is "1H Spread: Rockets (-5.5)").
- **The slug date is the Eastern game date, not UTC.** Checked all 52 games: 25 tip after
  midnight UTC and every one of those slugs carries the ET date (e.g. `nba-gsw-lal-2026-10-21`
  tips at `2026-10-22 02:00 UTC`). This is the opposite of the NFL, where all 10 night games
  carry the UTC date. Kalshi uses the ET date for both sports, so for the NBA the Polymarket slug
  date equals the Kalshi ticker date with no offset. Treat the slug-date convention as a
  per-sport parameter, never a constant.
- 30 slug codes: atl bos bkn cha chi cle dal den det gsw hou ind lac lal mem mia mil min nop
  nyk okc orl phi phx por sac sas tor uta was. Kalshi uses the same 30 codes uppercased
  (verified from all 102 open `KXNBAGAME` markets).
- Nicknames used in titles: Hawks, Celtics, Nets, Hornets, Bulls, Cavaliers, Mavericks,
  Nuggets, Pistons, Warriors, Rockets, Pacers, Clippers, Lakers, Grizzlies, Heat, Bucks,
  Timberwolves, Pelicans, Knicks, Thunder, Magic, 76ers, Suns, Trail Blazers (two words),
  Kings, Spurs, Raptors, Jazz, Wizards.
- Kalshi city strings as used in its titles: Atlanta, Brooklyn, Boston, Charlotte, Chicago,
  Cleveland, Dallas, Denver, Detroit, Golden State, Houston, Indiana, Los Angeles C,
  Los Angeles L, Memphis, Miami, Milwaukee, Minnesota, New Orleans, New York (= Knicks),
  Oklahoma City, Orlando, Philadelphia, Phoenix, Portland, Sacramento, San Antonio, Toronto,
  Utah, Washington.

Family inventory (markets): total 106 | moneyline (bare game slug) 52 | odd-even 52 |
team-to-score-first 52 | spread-home 50 | spread-away 20 | 1h-total 6 | 1h-moneyline 5 |
1h-spread-home 4 | 1h-spread-away (seen) | player props `points-`, `rebounds-`, `assists-`
followed by a player slug and a line, several hundred rows.

Title grammar, with outcomes and Gamma `sportsMarketType`:

```
Hawks vs. Hornets                        [Hawks, Hornets]      moneyline (away first)
Spread: Celtics (-10.5)                  [Celtics, Pistons]    spreads (favorite first)
Hawks vs. Magic: O/U 230.5               [Over, Under]         totals
Mavericks vs. Rockets: 1H Moneyline      [Mavericks, Rockets]  first_half_moneyline
1H Spread: Rockets (-5.5)                [Rockets, Mavericks]  first_half_spreads
Mavericks vs. Rockets: 1H O/U 118.5      [Over, Under]         first_half_totals
Hawks vs. Hornets: Odd/Even Score        [Odd, Even]           basketball_odd_even
Hawks vs. Hornets: Team to Score First   [Hawks, Hornets]      basketball_team_to_score_first
CJ McCollum: Points O/U 16.5             [Yes, No]             points | rebounds | assists
```

Watch the last row: player props use **Yes/No**, where Yes means over. Team markets use
Over/Under. Do not assume one convention.

Gamma `line` is negative for the favorite in both `spread-home` and `spread-away` examples
(Celtics away -10.5, Magic home -2.5), so the sign does not encode home or away; the slug token
does. Verify over all rows and document.

Settlement rules verified from Gamma descriptions:
- Full-game moneyline, spread, total, odd/even: final score **including overtime**.
- 1H markets: halftime score only. **1H moneyline resolves 50-50 on a halftime tie.**
  Kalshi's `KXNBA1H` is three-way with a separate TIE market, so a Polymarket 1H moneyline
  outcome is never `exact` against Kalshi's "wins the 1st half" (tie pays 0.5 on one venue,
  0 on the other). This must surface as a note.
- Player props: whole game including OT; a player listed inactive or who never takes the court
  resolves **No**. Compare against Kalshi's `rules_secondary` for the same case.
- Postponed: stays open until played. Cancelled: 50-50. Kalshi: stays open if played within
  48 hours, otherwise "resolves to a fair price". Record this as a standing note.
- Spread wording varies between markets ("win by 11 or more" vs "wins by more than 2.5");
  the semantics are the same, parse the number from the title and slug, not the prose.

Gamma (public, curl works; plain Python urllib gets 403, send a browser User-Agent):
`GET https://gamma-api.polymarket.com/markets?slug={slug}` and
`GET https://gamma-api.polymarket.com/events?slug=nba-gsw-lal-2026-10-21` (all markets of a game).

## Kalshi NBA facts (verified against the public API on 2026-10-20)

Base `https://api.elections.kalshi.com/trade-api/v2`, no auth for reads. Rate limit is real:
bursts of about one request per second returned 429. Space requests and use `limit=200`
(`KXNBAGAME` returns all open markets in one page). `GET /series?category=Sports&tags=Basketball`
lists 681 series, 356 with NBA in the ticker; most are season or award markets.

Ticker grammar: `{SERIES}-{YY}{MON}{DD}{AWAY}{HOME}-{SUFFIX}`, ET date. Suffix integer is
`ceil(line)`; the authoritative line is `floor_strike`.

Game series verified live:

```
KXNBAGAME-26OCT26GSWDEN-GSW            Golden State wins
KXNBASPREAD-26OCT23HOUSAS-SAS9         San Antonio wins by over 8.5     floor_strike 8.5
KXNBATOTAL-26OCT23HOUSAS-254           Full Game: Over 253.5            floor_strike 253.5
KXNBATEAMTOTAL-26OCT21SACLAC-SAC99     Sacramento over 98.5             floor_strike 98.5
KXNBA1HSPREAD / KXNBA1HTOTAL / KXNBA1HTEAMTOTAL   1st half
KXNBA2HSPREAD / KXNBA2HTOTAL           2nd half, rules say "including overtime"
KXNBA1QSPREAD / KXNBA1QTOTAL           1st quarter
KXNBA4QSPREAD                          4th quarter, rules say "excluding overtime"
KXNBA1H / KXNBA1Q / KXNBA2Q            period winners, three-way with a -TIE market
KXNBAOT-26OCT21SACLAC-1                goes to overtime, floor_strike 0.5
KXNBARACE-26OCT21SACLAC-10-SAC         Sacramento reaches 10 points first
KXNBAWINMARGIN-26OCT21SACLAC-SAC6TO10  wins by 6 to 10
KXNBAPTS-26OCT20PHINYK-NYKMMCBRIDE2-15 Miles McBride: 15+ points, floor_strike 14.5
KXNBAREB / KXNBAAST / KXNBA3PT         same shape; suffix = TEAM + first initial + SURNAME + digit
KXNBAPTSLEADER                         game points leader
```

Listed but not probed: KXNBA2H, KXNBA3Q, KXNBA4Q, KXNBA2QSPREAD, KXNBA3QSPREAD, KXNBA2QTOTAL,
KXNBA3QTOTAL, KXNBA4QTOTAL, KXNBA1QTEAMTOTAL through 4Q, KXNBA2HTEAMTOTAL, KXNBAFIRSTBASKET
(no open markets today), KXNBAPRA, KXNBAPR, KXNBAPA, KXNBARA, KXNBASTL, KXNBABLK, KXNBAFTM,
KXNBA2D, KXNBA3D. No Kalshi counterpart was found for Polymarket's odd/even or
team-to-score-first; confirm and report.

Kalshi spreads are one-sided per team, as in the NFL: Polymarket "Spread: Celtics (-10.5)"
outcome Celtics == `BOS11` YES, and outcome Pistons == `BOS11` NO. A `DET11` market is a
different contract.

## Player props

Polymarket gives the player name and stat but **not the team**; Kalshi gives team, first initial,
surname, and a digit. Match deterministically only when all of these hold: same game, same stat,
same line (Polymarket 16.5 == Kalshi `floor_strike` 16.5, titled "17+"), and exactly one Kalshi
market in that game whose surname and first initial equal the Polymarket name after a fixed,
documented normalization (lowercase, strip punctuation, strip Jr / Sr / II / III / IV). Anything
else is `none`. Always attach a note about the did-not-play rule unless you have verified the
two venues agree. Put the normalization in `nba_players.py` with tests on the awkward names in
the catalog (Shai Gilgeous-Alexander, Nickeil Alexander-Walker, Trey Murphy III, De'Aaron Fox,
Michael Porter Jr).

## Deliverables

1. `nba_teams.py`: 30-team table (canonical code, city, nickname, Poly code, Kalshi code,
   Kalshi city string). Exact lookups, unknown raises.
2. `nba_poly.py`: `parse_poly_nba(slug, title, outcome, outcome_index) -> LegSpec`. Pure and
   offline. Cross-check title against slug (nickname vs code, period, line) and flag any
   disagreement as a parse error.
3. `nba_kalshi.py`: `parse_kalshi_nba(ticker, title, floor_strike, strike_type, rules_primary,
   rules_secondary, expected_expiration_time) -> LegSpec`, plus the list of NBA game series for
   the shared Kalshi client.
4. `nba_players.py`: name normalization and the single-candidate matching rule above.
5. Registration of the NBA in the shared registry once `totalis-47` exposes it, including the
   slug-date convention (ET) for the game key.
6. `nba_coverage.py`: percent parsed by family and period, every unparsed slug, match
   confidence counts by family, Polymarket families with no Kalshi counterpart, all distinct
   settlement notes; `--gamma` validates a sample against Gamma (`line`, `sportsMarketType`,
   `gameStartTime`, outcomes) and reports disagreements.
7. Tests: real fixtures for every family (both outcomes), ~20 real Kalshi tickers across series,
   the ET-date case (`nba-gsw-lal-2026-10-21` vs `KXNBAGAME-26OCT21GSWLAL`), the 1H-tie
   settlement note, the Yes/No-vs-Over/Under convention, player name normalization, and a test
   that parses every NBA row in `data/rfq.db` when present.
8. `claude-briefs/nba-matching-notes.md`: what you verified, what differs from this brief, open questions.

## Report back

Send `totalis-94` a short summary: coverage numbers by family, families with no Kalshi
counterpart, settlement differences found, player-prop match rate, and anything in the data that
contradicts this brief. Say plainly if something here is wrong.
