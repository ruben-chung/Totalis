# NFL identity + Kalshi matching: what was built and what the data says (2026-10-20)

Built against `claude-briefs/nfl-matching-brief.md`. Everything below was verified against the catalog
in `data/rfq.db` (24,958 NFL position rows / 12,479 markets / 45 games), the Gamma API and
the Kalshi public API on 2026-10-20.

## Coverage

* Polymarket: **24,958 / 24,958 rows parse (100%)**, zero unparsed slugs. 45 games, 22
  family/period combinations. A 60-market stratified Gamma sample agrees on line, market type,
  kickoff date, outcomes and position ids with 0 disagreements.
* Kalshi: 3,269 open markets across 30 live game-level series, all parse (0 errors).
  28 games (Kalshi lists a week or so ahead; Poly lists three weeks).
* Match confidence (position rows): exact 3,700 | rules_differ 144 | shifted_line 3,078 | none 18,036.
  Of the `none`, 7,920 rows are the 15 games Kalshi has not listed yet, ~7,000 are games where
  Kalshi has only the moneyline so far, 1,376 are 2H team totals (series exists, nothing open),
  and the rest are families with no Kalshi equivalent (below).

## Poly families with no Kalshi counterpart

* exact margin bands (`exact-margin-{long,short}-*`): nothing on Kalshi. `exact-margin-tie` == `KXNFLTIE`.
* highest scoring quarter: series `KXNFLHIGHSCOREQ` exists but has 0 open markets and an unknown
  ticker grammar, so no parse rule.
* 2H team totals (`KXNFL2HTEAMTOTAL`), team touchdowns (`KXNFLTEAMTD`), two-point conversions
  (`KXNFL2PTCONV`): series exist, no open markets.
* Kalshi series with no Poly family (not synced): 1H/FT double result, overtime, race-to-N,
  both-teams-N+, every-quarter BTTS, scoreless quarter, comeback, largest lead, field goals, TD counts.

## Settlement-rule differences found

* **Period moneylines** (1H/2H/1Q–4Q): Poly resolves 50-50 on a tie; Kalshi is three-way with a
  TIE strike (team strikes resolve No). Reported as `rules_differ`, never `exact`. 144 rows.
* Full-game moneyline: both venues pay 50-50 on a tie -> exact.
* OT: both venues agree per period (1H / 1Q–4Q exclude, 2H includes, 4Q "excluding overtime"
  explicit on both). Poly full-game spreads say "overtime is included"; Kalshi full-game
  spreads/totals/team totals say nothing (reported as `info:ot`). Poly full-game totals and team
  totals also say nothing.
* Cancellation / postponement: Poly stays open on postponement and resolves 50-50 on
  cancellation for every family (highest scoring quarter resolves "Other"). Kalshi winners,
  tie, BTTS, safety and team-yards: open if the game starts within 48 h, else "fair price".
  Kalshi spreads, totals and team totals carry **no** cancellation text at all. These are
  reported as `abnormal:` notes and do not block `exact` (they only matter for games that are
  not played; flagged so this can be revisited).

## Surprises relative to the brief

* Gamma `line` for spreads is `-X.5` for **both** `spread-home` and `spread-away`: it is the
  handicap of the team named in the title, so its sign carries no home/away information. The
  slug token is the only place home/away lives. LegSpec therefore stores `team` = the favoured
  team and `line` negative for it, same convention for Kalshi.
* Poly lists both-side spreads at the same line in the same game ("Spread: Cardinals (-10.5)"
  and "Spread: Cowboys (-10.5)" both exist). They are different contracts; the complement of
  "Cardinals -10.5 / Cardinals" is "Cardinals -10.5 / Cowboys" (= Cowboys +10.5), not the other market.
* 43 bare moneyline games in the catalog, not 14. 45 games total (two already played: PIT@TB
  Oct 18, SEA@DEN Oct 16, with a few leftover prop rows).
* `exact-margin-long-*` = away team wins, `short-*` = home team wins (126/126 rows). Band sets
  differ: away 1-6/7-12/13-18/19-24/25+, home 1-6/7-13/14-20/21+.
* Kalshi codes differ from Poly only for JAC (Jaguars), LAR (Rams), LAC (Chargers). Kalshi
  title cities: "Los Angeles R", "Los Angeles C", "New York G", "New York J". Kalshi pair order
  is away-home like Poly (28/28 live games).
* Kalshi spreads are one-sided and both teams get a ladder; the ladder skips 11.5, 12.5, 15.5,
  18.5, 19.5 (key numbers), so Poly lines at those values match as `shifted_line`.
* Kalshi `expected_expiration_time` is not a reliable kickoff proxy (same game, different
  series, hours apart); the ticker date is what carries the ET game date.
* Catalog tag `nfl` also appears as `nfl-gameday` on a handful of CFB rows; parsers key on the
  slug prefix, not the tag.
* `KXNFL1HWINNER`, `KXNFL{n}QWINNER`, `KXNFL2HWINNER` exist as series names but the live
  period winners are `KXNFL1H`, `KXNFL2H`, `KXNFL1Q`..`KXNFL4Q`. Both are registered.

## Game identity rule

`game_key = nfl|{ET date}|{AWAY}@{HOME}`. Kalshi tickers carry the ET date. Poly slugs carry the
UTC kickoff date (all 10 night games in the catalog are +1 day), so a slug-only Poly spec has
`game_date_basis="utc_slug"` and the matcher resolves it against the Kalshi index on the slug
date or the day before, asserting exactly one hit. With a Gamma `gameStartTime` the parser
emits the ET date directly. The slug convention is per sport (`sports/registry.py`): NBA slugs
are already ET.

## Things deliberately not done

Pricing, order books, other sports, re-running the catalog sync. Kalshi markets are fetched with
`status=open` only; `--all-statuses` exists for backfills.
