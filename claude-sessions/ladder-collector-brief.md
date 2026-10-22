# Brief: widen the book recorder to whole-game ladders, and expose them

Written 2026-10-20 by the planning session (now `totalis-4d`, formerly `totalis-94`).
Owner: Ruben. When done, send a summary back to `totalis-4d` via SendMessage.

## Why

The next pricing component (an NFL joint-distribution engine, briefed separately to
`totalis-47`, see `claude-sessions/joint-engine-brief.md`) reads each game's full line ladders: every
spread line, total line, team-total line and first-half line, for both sides. The catalog lists
them (NFL: 17 spread lines per side, 21 totals, 11 team-total lines per team, 16 1H spreads
per side, 5 1H totals, 6 1Q totals per game), but the catalog prices are placeholders, so the
ladders have to come from live books.

`rfqbot/books.py` already records CLOB top-of-book for legs seen in recent RFQ flow
(`RfqLegUniverse`, window `BOOK_UNIVERSE_HOURS`). That universe is per leg, so a game whose
Vikings -7.5 line appears in an RFQ gets that one token recorded and none of the other 33
spread lines. This task widens the universe to whole games and adds a read API for ladders.
If you did not write `books.py`, read it fully first; keep its design (raw gz log + change-only
`book_top` rows, TokenMap via catalog + Gamma, connections of at most 500 assets).

## Deliverables

1. **Game universe.** A second universe source, `GameUniverse`, that takes the leg position_ids
   from `RfqLegUniverse`, maps them to game prefixes via `markets.slug`
   (`{league}-{away}-{home}-{YYYY-MM-DD}`; use `rfqbot.sports.registry.sport_for_slug` for
   registered sports and the generic regex otherwise), and returns every position_id in
   `markets` whose slug starts with one of those prefixes. Configurable:
   `BOOK_GAME_SPORTS` (default `nfl,nba`: whole-game ladders only for these; everything else
   stays leg-level), `BOOK_GAME_MAX_TOKENS` (default 20000, games ranked by RFQ count in the
   window when the cap binds), `BOOK_GAME_LOOKAHEAD_HOURS` (skip games whose Gamma
   `gameStartTime` is further out than this; default 72). Both universes union into the
   subscription set. Report counts per sport at each refresh (log line + `bookstats`).
2. **Token resolution at game scale.** TokenMap currently resolves per position via Gamma in
   batches of 100. For whole games use `GET gamma-api.polymarket.com/events?slug={game}`
   (one call returns all markets of the game with `clobTokenIds`, `positionIds`,
   `gameStartTime`, `sportsMarketType`, `line`) and fill `leg_tokens` for every market in the
   game in one go. Gamma returns 403 to plain Python urllib without a browser User-Agent; httpx
   with a UA header works. Record `game_start_time` and `event_id` on every row (the columns
   exist).
3. **Ladder read API** in a new module `rfqbot/ladders.py` (you own it):

   ```python
   @dataclass(frozen=True)
   class MarketQuote:
       position_id: str; token_id: str; slug: str; title: str; outcome: str; outcome_index: int
       best_bid: float | None; best_ask: float | None      # probabilities 0..1
       bid_size: float | None; ask_size: float | None
       bid_depth: float | None; ask_depth: float | None    # shares within 0.05 of best
       at_ms: int                                          # time of the book_top row used
       game_start_time: str | None

   def game_markets(game_prefix: str, at_ms: int | None = None, *, books_db=..., rfq_db=...) -> list[MarketQuote]
   ```

   `game_prefix` is the Polymarket slug prefix. With `at_ms=None` return the latest `book_top`
   row per token; with `at_ms` return the latest row at or before that time (point-in-time, for
   the backtest; no future rows). Include markets that have a token but no book row yet, with
   None prices, so the caller can see the gap. Keep it a plain read-only function over sqlite,
   fast enough to call once per game per rebuild (index `book_top(asset_id, at_ms)` exists).
4. **`bookstats` additions**: per sport, number of games covered, tokens subscribed, share of
   tokens with at least one `book_top` row in the last 10 minutes, and the ten games with the
   most RFQs that have the lowest book coverage.
5. **Tests** alongside the existing `tests/test_books.py` style: GameUniverse expansion and
   cap, Gamma event-based token fill (mocked), `game_markets` latest and point-in-time
   semantics (no future rows), and the union with the leg universe.

## Ownership and coordination

You own `rfqbot/books.py`, `rfqbot/ladders.py`, the book settings in `rfqbot/config.py`,
`tests/test_books.py`, `tests/test_ladders.py`. `rfqbot/__main__.py` is owned by `totalis-47`;
send it the subcommand/flag changes you need as a snippet rather than editing the file. The
repo is not a git repository, so do not touch files you do not own. `totalis-47` builds the
engine against the `game_markets` signature above; if you need to change that signature, tell
it first.

## Facts you will need

- RFQ position_ids are not CLOB token ids; `leg_tokens` maps them (already handled).
- `data/books.db` is already ~80 MB after a few hours at leg scale. Whole-game NFL+NBA adds
  roughly 45 games x 280 markets x 2 tokens for the NFL and 52 x ~30 x 2 for the NBA, about
  28k tokens if uncapped; the default cap of 20k plus the lookahead filter keeps it sane.
  Change-only writes mean quiet far-ladder lines cost almost nothing.
- The market channel accepts dynamic subscribe/unsubscribe; use it rather than reconnecting.
- Current repo state: 84 tests green (`.venv/bin/python -m pytest -q`). Keep them green.

## Report back

Tokens and games covered per sport, book coverage share, any Gamma or websocket limits hit,
and the final `game_markets` signature if it differs from the above.
