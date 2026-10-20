# NBA matching layer: notes

Written 2026-10-20 by the NBA build session (totalis-c8) for the planning session (totalis-94) and Ruben.
Brief: `docs/nba-matching-brief.md`. Code: `rfqbot/sports/nba_*.py`, registration in `rfqbot/sports/nba.py`.
Run: `.venv/bin/python -m rfqbot.sports.nba_coverage [--kalshi] [--gamma N]` (standalone; the shared
`python -m rfqbot coverage --sport nba` and `kalshi-sync --sport nba` also work through the registry).

## Coverage (catalog as of 2026-10-20 evening, Kalshi open markets at the same time)

Polymarket: 1,488 position rows, 744 markets, 52 games, **100% parsed, 0 unparsed**. Duplicate
canonical keys: 0. Rows tagged `nba` with a non-`nba-` slug: 0.

| family / period | rows | exact | rules_differ | shifted_line | none |
|---|---|---|---|---|---|
| moneyline FG | 104 | 102 | 0 | 0 | 2 |
| moneyline 1H | 10 | 0 | 10 | 0 | 0 |
| spread FG+1H | 160 | 114 | 0 | 4 | 42 |
| total FG+1H | 224 | 92 | 0 | 88 | 44 |
| odd_even FG | 104 | 0 | 0 | 0 | 104 |
| team_to_score_first FG | 104 | 0 | 0 | 0 | 104 |
| player_points | 276 | 0 | 40 | 152 | 84 |
| player_rebounds | 268 | 0 | 106 | 80 | 82 |
| player_assists | 238 | 0 | 84 | 78 | 76 |
| **total** | 1,488 | 308 | 240 | 402 | 538 |

(First run; the second run after the player-key change is in the coverage output and differs only
in player-prop rows, see below.) All 52 Polymarket games have a Kalshi game on the same date.

Kalshi: 30 non-empty series synced, 4,729 markets (4,851 an hour earlier; markets close at tip),
**0 parse errors**. Six registered series had no open markets (1Q..4Q and 2H team totals, FIRSTBASKET).

The "none" rows for spread / total / moneyline are games that Kalshi had not yet listed that
family for (10-24 to 10-26 games), not grammar gaps. 2 moneyline rows: one game (Oct 26) had no
KXNBAGAME market yet.

## Families with no Kalshi counterpart

- `odd_even` (104 rows) and `team_to_score_first` (104 rows): no Kalshi series. `KXNBARACE` is
  "first to N points" with N >= 10, `KXNBAFIRSTBASKET` exists but had zero open markets so its
  grammar is unknown (probably a player market). Confirmed as in the brief.
- Kalshi series with no Polymarket family (listed, not synced): KXNBAOT, KXNBARACE, KXNBAWINMARGIN,
  KXNBAPTSLEADER, KXNBA2D, KXNBA3D, KXNBAFIRSTBASKET, and the empty KXNBAPR / KXNBAPA / KXNBARA /
  KXNBAFTM (the last four were not in the brief).
- Kalshi has 2H / 1Q-4Q spreads, totals and period winners, and team totals; Polymarket has none
  of those for the NBA (only FG and 1H).

## Settlement differences found (distinct notes, rows affected, first run)

| note | rows | meaning |
|---|---|---|
| `payoff:dnp: poly no, kalshi fair_price` | 540 | player props: inactive / never takes the court -> Poly resolves **No**, Kalshi settles at a **fair price**. Player props are therefore never `exact`, at best `rules_differ`. The brief said "compare"; they do not agree. |
| `payoff:tie: poly 50-50, kalshi tie_strike` | 10 | 1H moneyline: Poly pays 0.5 on a halftime tie, Kalshi KXNBA1H team strike pays 0 (separate TIE market). As the brief predicted. |
| `info:group: poly binary, kalshi three_way_with_tie_strike` | 10 | same markets |
| `info:ot: poly included, kalshi unstated` | 642 | FG moneyline, odd/even and player props say "including any/all overtime periods" on Poly; Kalshi's FG rules never mention OT. Not payoff-relevant in practice (an NBA final score includes OT by definition) but recorded as the text says. |
| `abnormal:cancel: poly 50-50, kalshi fair_price_if_not_started_48h` | 940 | Poly: cancelled with no make-up -> 50-50. Kalshi: not started within 48 h -> "fair price". |
| `abnormal:postpone: poly stays_open, kalshi open_if_started_within_48h` | 940 | Poly stays open until played, Kalshi only for 48 h. |
| `abnormal:cancel/postpone: ... kalshi unstated` | 10 each | Kalshi 1H spread markets carry no postponement text at all. |

## Player props

Single-candidate rule (`nba_players.match_player_prop`): same game, same stat, same line, exactly
one Kalshi market whose first initial + surname equals the Polymarket name's. First run: 782 legs,
232 matched (29.7%), 550 unmatched: 314 "player listed but not at this line" (Kalshi ladders are
5 points apart for points and 2 apart for rebounds/assists, Polymarket uses the book line), 176
"player not listed on Kalshi for this stat", 60 in games Kalshi had no props for yet. The shared
matcher offers a shifted line for 310 of the 550.

Name normalization verified on every awkward name in the catalog (Gilgeous-Alexander,
Alexander-Walker, Murphy III, De'Aaron Fox, Porter Jr., Sengün, Dončić, Day'Ron Sharpe, P.J.
Washington, Karl-Anthony Towns, OG Anunoby, LaVine, VanVleet) and on all 2,015 Kalshi player
markets (ticker initial+surname == title name after normalization, 0 failures).

Finding: the venues differ on first names ("Alexandre Sarr" on Polymarket, "Alex Sarr" on Kalshi).
With a full-name key those two rows were `none`; with the brief's initial+surname identity they
match. So `LegSpec.bucket` for player props is `initial.surname` (`a.sarr`) on both venues, and
the Kalshi side carries `team=None` so contract keys agree (the Kalshi team and jersey number are
kept in `venue_ids`). The cost: two players sharing initial+surname in one game would share a
bucket. `nba_players.bucket_collisions` finds those, the coverage report prints them, and
`match_player_prop` refuses them; none exist in today's data on either venue (Jalen / Jaylin
Williams would be the live example if both get markets).

## What differs from the brief

1. **Kalshi player suffix digit is a jersey number, one or more digits**, not a single digit:
   `ORLWCARTER34`, `LALLDONCIC77`, `SACDSABONIS11`. Parsed as `{TEAM}{INITIAL}{SURNAME}{JERSEY}-{N}`.
2. **Full-game spread descriptions on Polymarket use two templates.** "wins by more than 2.5
   points ... including any overtime periods" and "win the game by 11 or more points" with no OT
   sentence, on both `spread-home` and `spread-away`. Full-game totals never mention OT. So the
   brief's "spread, total: including overtime" is only true for one template; the parser records
   `ot=unstated` for FG spreads and totals (the catalog row cannot see the description) and
   `--gamma` reports how many sampled descriptions state it.
3. **Kalshi full-game moneyline has no tie rule** (NBA games cannot tie), so the FG moneyline tie
   is `n/a` on both venues; only the 1H moneyline has the 50-50 vs tie-strike difference.
4. **Did-not-play differs** (Poly No, Kalshi fair price). The brief asked to compare; they differ.
5. **Kalshi 1H markets' rules_secondary says "2nd half markets are inclusive of overtime"**, a
   boilerplate note that appears on 1H markets too; the parser reads the period from
   rules_primary ("in the 1st half of the"), not from that note.
6. Kalshi quarter totals have a doubled space in rules_primary ("1st quarter  of the"); handled.
7. Four extra empty series (KXNBAPR, KXNBAPA, KXNBARA, KXNBAFTM) exist beyond the brief's list.
8. Several catalog slugs are **not found on Gamma** by `/markets?slug=` (e.g.
   `nba-bos-det-2026-10-20-total-224pt5`, several `spread-away`), presumably lines that were
   retired from the event. The parser does not need Gamma, so this only affects `--gamma`.
9. Everything else in the brief checked out: ET slug date (the matcher needed no day shift for
   any of the 52 games), away-home order, 30 codes identical on both venues, nicknames, Kalshi
   city strings ("Los Angeles C/L", "New York" = Knicks), suffix integer = ceil(line),
   one-sided Kalshi spreads, Yes/No vs Over/Under conventions.

## Historical title grammar (added 2026-10-20 evening)

`data/history.db` (Dec 2025 - Jun 2026) titles player props "X: Points Over 13.5" until about
mid-January 2026 and "X: Points O/U 13.5" after; outcomes are Yes/No in both (7,697 "Over" rows,
none titled "Under"). Gamma (with `closed=true`) shows the same description for the old form:
"Yes if X records more than 2.5 assists", OT included, did-not-play -> No, cancel 50-50. The parser
accepts both forms and emits identical LegSpecs (fixture `tests/fixtures/nba_poly_hist_rows.json`).

## Slug date convention is not constant (found by the Data Engineer, 2026-10-20 evening)

Per game in `data/history.db` (Gamma `gameStartTime`): 92 games between 2025-10-27 and 2025-11-11
carry the **UTC** date of the tip in the slug (tip hours 00:00 to 03:30 UTC, i.e. real evening ET
games), interleaved with ET-dated slugs in the same weeks (October: 23 UTC-dated vs 52 ET-dated
among games where the two dates differ). From mid-November 2025 on, every slug is ET-dated, and so
is all current data. The parser now accepts either when a kickoff is supplied, keys the game on
the ET date and records the convention in `venue_ids["slug_date"]`; a slug matching neither (the
two real reschedules, +52 and +66 days) remains an error. Slug-only parses still assume ET.

## Gamma validation (`--gamma 60`, 2026-10-20)

Stratified sample: one market per family/period (11) plus 49 random. **60 checked, 0
disagreements** on sportsMarketType, line, gameStartTime vs ET slug date, outcomes, position ids,
OT wording, did-not-play sentence, 1H tie sentence, spread favourite, cancellation and
postponement sentences. 1 catalog slug not found on Gamma (`nba-bos-det-2026-10-20-total-220pt5`,
a retired line). Among the full-game spread/total markets sampled, 1 of 3 spreads and 1 of 4
totals state overtime in the description; the rest do not (hence `ot=unstated` for those families).

## Open questions

- **Decided 2026-10-20 (planning session):** for pricing, treat `ot=unstated` on any full-game
  market as "included" on both venues (NBA games cannot end tied; every FG market settles on the
  final score). The parser keeps recording `unstated` so the assumption stays visible; the
  matcher keeps it as `info:`, which does not block `exact`.
- Deferred, no work needed now: KXNBAFIRSTBASKET grammar (no open markets to inspect); WNBA
  (152 catalog rows, same slug grammar with a `wnba-` prefix, needs its own team table via
  `nba_teams.LEAGUES` and a check of Kalshi's WNBA series).
- Postponement window: Poly "stays open until played" vs Kalshi 48 h is the standing abnormal
  note on every matched row; a hedge across venues is exposed to it.
