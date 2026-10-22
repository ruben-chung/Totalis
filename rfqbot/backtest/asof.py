"""As-of readers over the two book stores: what the quoting path would have seen at a pinned time.

``GameCache`` takes ``poly`` and ``kalshi`` readers and calls ``compute_game(prefix, None, now_ms=clock)``;
the readers here answer "the row with the largest ``at_ms <= t``" from arrays loaded once per game, with
``t`` the replay clock, so no row after ``t`` can be returned (tests insert a row at ``t + 1`` and check).
They mirror ``rfqbot.ladders.game_markets`` and ``rfqbot.kalshi_books.kalshi_for_legs`` field for field
(tested against both on the same fixture) and expose ``.confirm`` so ``compute_game`` measures staleness
from the feed heartbeat as of ``t`` (the stores are change-only; see claude-sessions/marginal-layer-notes.md).

Limits, recorded in the notes: ``leg_matches`` holds only the current match per leg (the matcher is a pure
function of the catalog, so matches do not drift inside a window); the catalog (``markets``) is the one
at the end of the recording.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from ..kalshi_books import KalshiQuote, MatchRow
from ..ladders import MarketQuote

PRICE_SCALE = 10_000


def ro(path: str | Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, check_same_thread=False, timeout=60)
    c.row_factory = sqlite3.Row
    return c


class Clock:
    """The pinned replay clock; callable like ``time`` in ms."""

    def __init__(self, t_ms: int = 0):
        self.t = int(t_ms)

    def __call__(self) -> int:
        return self.t

    def set(self, t_ms: int) -> None:
        if t_ms < self.t:
            raise ValueError(f"clock moved backwards: {t_ms} < {self.t}")
        self.t = int(t_ms)


def _nan(x: float) -> float | None:
    return None if x != x else float(x)


class Heartbeat:
    """Sorted distinct ``at_ms`` of a whole table: the newest feed activity at or before t."""

    def __init__(self, ts: Iterable[int]):
        self.ts = np.fromiter(ts, dtype=np.int64)
        self.ts.sort()

    @classmethod
    def from_query(cls, conn: sqlite3.Connection, sql: str, *args) -> "Heartbeat":
        rows = conn.execute(sql, args).fetchall()
        return cls(r[0] for r in rows)

    def at(self, t_ms: int) -> int | None:
        i = int(np.searchsorted(self.ts, t_ms, side="right")) - 1
        return None if i < 0 else int(self.ts[i])


@dataclass
class _Series:
    """One instrument's change-only rows, sorted by time."""
    at: np.ndarray                 # int64
    cols: dict[str, np.ndarray]    # float64, NaN for NULL
    labels: list[str] | None = None   # per-row text column (Kalshi `source`)

    def idx(self, t_ms: int) -> int:
        return int(np.searchsorted(self.at, t_ms, side="right")) - 1


def _series(rows: list[tuple], names: tuple[str, ...]) -> _Series:
    arr = np.array([[np.nan if v is None else v for v in r[1:]] for r in rows], dtype=np.float64) if rows else np.zeros((0, len(names)))
    return _Series(np.array([r[0] for r in rows], dtype=np.int64), {n: arr[:, i] for i, n in enumerate(names)})


# ------------------------------------------------------------------ Polymarket
POLY_COLS = ("best_bid", "best_ask", "bid_size", "ask_size", "bid_depth", "ask_depth")
BIG = 1 << 42          # > any epoch ms; token_index * BIG + at_ms sorts all of a game's rows by (token, time) in one array


class GameData:
    """One game's ladder history in memory: every member token's change-only rows, as-of lookups vectorized.

    ``positions(t)`` answers "the row with the largest at_ms <= t" for every token with one searchsorted over
    the composite key ``token_index * BIG + at_ms``; -1 where the token has no row yet."""

    def __init__(self, prefix: str, members: list[tuple[Any, Any]], rows: dict[str, np.ndarray],
                 subs: dict[str, list[tuple[int, int | None]]]):
        self.prefix = prefix
        self.members = members
        self.pids = [r["position_id"] for r, _ in members]
        self.pid_index = {p: i for i, p in enumerate(self.pids)}
        self.token_ids = [t["token_id"] for _, t in members]
        self.subs = subs
        M = len(members)
        blocks, starts, keys, ats = [], np.zeros(M, dtype=np.int64), [], []
        n = 0
        for i, tok in enumerate(self.token_ids):
            starts[i] = n
            a = rows.get(tok)
            if a is not None and len(a):
                at = a[:, 0].astype(np.int64)
                keys.append(i * BIG + at)
                ats.append(at)
                blocks.append(a[:, 1:])
                n += len(a)
        self.start = starts
        self.key = np.concatenate(keys) if keys else np.zeros(0, dtype=np.int64)
        self.at = np.concatenate(ats) if ats else np.zeros(0, dtype=np.int64)
        self.cols = np.concatenate(blocks) if blocks else np.zeros((0, len(POLY_COLS)))
        self.n_rows = n
        self.first_row_ms = int(self.at.min()) if n else None

    def positions(self, t_ms: int) -> np.ndarray:
        if not self.n_rows:
            return np.full(len(self.members), -1, dtype=np.int64)
        q = np.arange(len(self.members), dtype=np.int64) * BIG + int(t_ms)
        pos = np.searchsorted(self.key, q, side="right") - 1
        return np.where(pos >= self.start, pos, -1)

    def quote(self, i: int, pos: int) -> MarketQuote:
        r, tok = self.members[i]
        base = dict(position_id=r["position_id"], token_id=tok["token_id"], slug=r["slug"] or "", title=r["title"] or "",
                    outcome=r["outcome"] or "", outcome_index=int(r["outcome_index"] or 0), game_start_time=tok["game_start_time"])
        if pos < 0:
            return MarketQuote(best_bid=None, best_ask=None, bid_size=None, ask_size=None, bid_depth=None, ask_depth=None, at_ms=0, **base)
        c = self.cols[pos].tolist()
        return MarketQuote(best_bid=None if c[0] != c[0] else int(c[0]) / PRICE_SCALE, best_ask=None if c[1] != c[1] else int(c[1]) / PRICE_SCALE,
                           bid_size=_nan(c[2]), ask_size=_nan(c[3]), bid_depth=_nan(c[4]), ask_depth=_nan(c[5]), at_ms=int(self.at[pos]), **base)

    def quotes(self, t_ms: int) -> list[MarketQuote]:
        pos = self.positions(t_ms)
        return [self.quote(i, int(p)) for i, p in enumerate(pos)]

    def mids(self, t_ms: int, max_spread: float = 0.1) -> dict[str, float]:
        """position_id -> mid for two-sided books no wider than ``max_spread`` (the cache's model-rebuild trigger)."""
        pos = self.positions(t_ms)
        ok = pos >= 0
        if not ok.any():
            return {}
        idx = np.flatnonzero(ok)
        c = self.cols[pos[idx]]
        bid, ask = np.rint(c[:, 0]) / PRICE_SCALE, np.rint(c[:, 1]) / PRICE_SCALE
        good = ~np.isnan(bid) & ~np.isnan(ask) & ((ask - bid) <= max_spread)
        mid = (bid + ask) / 2
        return {self.pids[int(i)]: float(m) for i, m, g in zip(idx, mid, good) if g}

    def subscribed(self, i: int, t_ms: int) -> bool:
        return any(s <= t_ms and (u is None or u > t_ms) for s, u in self.subs.get(self.token_ids[i], ()))


class PolyAsOf:
    """``read(prefix, at_ms=None)`` -> list[MarketQuote] as of ``at_ms`` (default: the clock); ``confirm``; ``game(prefix)``."""

    def __init__(self, books_db: str | Path, rfq_db: str | Path, clock: Clock | None = None, *, end_ms: int | None = None):
        self.books_db, self.rfq_db = Path(books_db), Path(rfq_db)
        self.clock = clock
        self.end_ms = end_ms
        self._games: dict[str, GameData] = {}
        self._hb: Heartbeat | None = None
        self._pre: dict[str, np.ndarray] | None = None      # token_id -> rows, from preload()
        self.loads = 0
        self.load_s = 0.0

    def preload(self, sports: Iterable[str] = ("nfl", "nba"), progress: Callable[[str], None] | None = None) -> int:
        """One sequential scan of book_top for every token of the given sports up to ``end_ms``.

        Per-token index lookups are random reads on a table the recorder keeps writing (35-65 s per game under
        load); one pass over the table in rowid (= time) order costs ~40 s for all of them. Returns the row count.
        """
        t0 = time.time()
        books = ro(self.books_db)
        try:
            likes = " OR ".join("slug LIKE ?" for _ in sports)
            books.execute("CREATE TEMP TABLE want AS SELECT DISTINCT token_id FROM leg_tokens WHERE token_id IS NOT NULL AND (" + likes + ")",
                          [f"{sp}-%" for sp in sports])
            books.execute("CREATE INDEX temp.ix_want ON want(token_id)")
            end = self.end_ms if self.end_ms is not None else 1 << 62
            per: dict[str, list[tuple]] = {}
            n = 0
            cur = books.execute("SELECT asset_id, at_ms, best_bid, best_ask, bid_size, ask_size, bid_depth, ask_depth FROM book_top NOT INDEXED"
                                " WHERE at_ms <= ? AND asset_id IN (SELECT token_id FROM want)", (end,))
            for r in cur:
                per.setdefault(r[0], []).append(r[1:])
                n += 1
            out = {}
            for tid, rows in per.items():
                a = np.array(rows, dtype=np.float64)
                if len(a) > 1 and (np.diff(a[:, 0]) < 0).any():
                    a = a[np.argsort(a[:, 0], kind="stable")]
                out[tid] = a
            self._pre = out
        finally:
            books.close()
        self.load_s += time.time() - t0
        if progress:
            progress(f"  preloaded {n} book rows for {len(out)} tokens in {time.time() - t0:.0f}s")
        return n

    # -- loading -----------------------------------------------------------------------
    def heartbeat(self) -> Heartbeat:
        if self._hb is None:
            c = ro(self.books_db)
            try:
                if self.end_ms is None:
                    self._hb = Heartbeat.from_query(c, "SELECT DISTINCT at_ms FROM book_top")
                else:
                    self._hb = Heartbeat.from_query(c, "SELECT DISTINCT at_ms FROM book_top WHERE at_ms <= ?", self.end_ms)
            finally:
                c.close()
        return self._hb

    def game(self, prefix: str) -> GameData:
        g = self._games.get(prefix)
        if g is not None:
            return g
        t0 = time.time()
        self.loads += 1
        cat, books = ro(self.rfq_db), ro(self.books_db)
        try:
            rows = cat.execute("SELECT position_id, slug, title, outcome, outcome_index FROM markets WHERE slug = ? OR slug LIKE ?"
                               " ORDER BY slug, outcome_index", (prefix, f"{prefix}-%")).fetchall()
            pids = [r["position_id"] for r in rows]
            tokens: dict[str, sqlite3.Row] = {}
            for i in range(0, len(pids), 500):
                chunk = pids[i:i + 500]
                for t in books.execute("SELECT position_id, token_id, game_start_time FROM leg_tokens WHERE token_id IS NOT NULL"
                                       f" AND position_id IN ({','.join('?' * len(chunk))})", chunk):
                    tokens[t["position_id"]] = t
            members = [(r, tokens[r["position_id"]]) for r in rows if r["position_id"] in tokens]
            per: dict[str, np.ndarray] = {}
            subs: dict[str, list[tuple[int, int | None]]] = {}
            end = self.end_ms if self.end_ms is not None else 1 << 62
            for _, tok in members:
                tid = tok["token_id"]
                if tid in per:
                    continue
                if self._pre is not None:
                    if tid in self._pre:
                        per[tid] = self._pre[tid]
                    subs[tid] = [(r[0], r[1]) for r in books.execute("SELECT subscribed_ms, unsubscribed_ms FROM subscriptions WHERE asset_id = ?", (tid,))]
                    continue
                got = books.execute("SELECT at_ms, best_bid, best_ask, bid_size, ask_size, bid_depth, ask_depth FROM book_top"
                                    " WHERE asset_id = ? AND at_ms <= ? ORDER BY at_ms", (tid, end)).fetchall()
                if got:
                    per[tid] = np.array(got, dtype=np.float64)
                subs[tid] = [(r[0], r[1]) for r in books.execute("SELECT subscribed_ms, unsubscribed_ms FROM subscriptions WHERE asset_id = ?", (tid,))]
            g = GameData(prefix, members, per, subs)
            self._games[prefix] = g
            return g
        finally:
            cat.close(); books.close()
            self.load_s += time.time() - t0

    # -- reads -------------------------------------------------------------------------
    def _t(self, at_ms: int | None) -> int:
        if at_ms is not None:
            return int(at_ms)
        if self.clock is None:
            raise ValueError("PolyAsOf needs an explicit at_ms or a clock")
        return self.clock()

    def read(self, prefix: str, at_ms: int | None = None) -> list[MarketQuote]:
        return self.game(prefix).quotes(self._t(at_ms))

    __call__ = read

    def confirm(self, quotes: list[MarketQuote], at_ms: int | None = None) -> dict[str, int]:
        """position_id -> newest feed activity at or before t, for tokens subscribed at t (marginals.sources.poly_confirmations)."""
        t = self._t(at_ms)
        if not quotes:
            return {}
        hb = self.heartbeat().at(t)
        if hb is None:
            return {}
        out = {}
        g = self._owner(quotes[0].position_id)
        for q in quotes:
            i = g.pid_index.get(q.position_id) if g else None
            if i is not None and g.subscribed(i, t):
                out[q.position_id] = hb
        return out

    def _owner(self, pid: str) -> GameData | None:
        for g in self._games.values():
            if pid in g.pid_index:
                return g
        return None

    def confirmed_ms(self, prefix: str, pid: str, t: int) -> int | None:
        g = self.game(prefix)
        i = g.pid_index.get(pid)
        if i is None or not g.subscribed(i, t):
            return None
        return self.heartbeat().at(t)

    def first_row_ms(self, prefix: str) -> int | None:
        """Time of the game's first book row (RFQs before it have no fair)."""
        return self.game(prefix).first_row_ms


# ------------------------------------------------------------------ Kalshi
KALSHI_COLS = ("yes_bid", "yes_ask", "bid_size", "ask_size", "bid_depth", "ask_depth", "server_ts_ms")


class KalshiAsOf:
    """``read(position_ids, at_ms=None)`` -> {position_id: (MatchRow, KalshiQuote | None)} as of ``at_ms``."""

    def __init__(self, kalshi_db: str | Path, clock: Clock | None = None, *, end_ms: int | None = None):
        self.kalshi_db = Path(kalshi_db)
        self.clock = clock
        self.end_ms = end_ms
        self._matches: dict[str, MatchRow | None] = {}
        self._series: dict[str, _Series | None] = {}
        self._hb: Heartbeat | None = None
        self._pre_done = False

    def preload(self, progress: Callable[[str], None] | None = None) -> int:
        """One sequential scan of kalshi_top for every ticker that any leg is matched to (same reason as PolyAsOf.preload)."""
        t0 = time.time()
        c = ro(self.kalshi_db)
        try:
            c.execute("CREATE TEMP TABLE want AS SELECT DISTINCT kalshi_ticker AS ticker FROM leg_matches WHERE kalshi_ticker IS NOT NULL")
            c.execute("CREATE INDEX temp.ix_want ON want(ticker)")
            end = self.end_ms if self.end_ms is not None else 1 << 62
            per: dict[str, list[tuple]] = {}
            n = 0
            for r in c.execute("SELECT ticker, at_ms, yes_bid, yes_ask, bid_size, ask_size, bid_depth, ask_depth, server_ts_ms, source FROM kalshi_top NOT INDEXED"
                               " WHERE at_ms <= ? AND ticker IN (SELECT ticker FROM want)", (end,)):
                per.setdefault(r[0], []).append(r[1:])
                n += 1
            for tk, rows in per.items():
                rows.sort(key=lambda x: x[0])
                a = np.array([x[:-1] for x in rows], dtype=np.float64)
                self._series[tk] = _Series(a[:, 0].astype(np.int64), {nm: a[:, i + 1] for i, nm in enumerate(KALSHI_COLS)}, [x[-1] or "" for x in rows])
            for (tk,) in c.execute("SELECT ticker FROM want"):
                self._series.setdefault(tk, None)
        finally:
            c.close()
        self._pre_done = True
        if progress:
            progress(f"  preloaded {n} kalshi rows for {len(per)} tickers in {time.time() - t0:.0f}s")
        return n

    def _t(self, at_ms: int | None) -> int:
        if at_ms is not None:
            return int(at_ms)
        if self.clock is None:
            raise ValueError("KalshiAsOf needs an explicit at_ms or a clock")
        return self.clock()

    def _load_matches(self, position_ids: Iterable[str]) -> None:
        need = [p for p in set(position_ids) if p not in self._matches]
        if not need:
            return
        c = ro(self.kalshi_db)
        try:
            for i in range(0, len(need), 500):
                chunk = need[i:i + 500]
                found = {}
                for r in c.execute(f"SELECT * FROM leg_matches WHERE position_id IN ({','.join('?' * len(chunk))})", chunk):
                    found[r["position_id"]] = MatchRow.from_row(r)
                for p in chunk:
                    self._matches[p] = found.get(p)
        finally:
            c.close()

    def _load_series(self, tickers: Iterable[str]) -> None:
        need = [t for t in set(tickers) if t not in self._series]
        if not need:
            return
        c = ro(self.kalshi_db)
        try:
            for i in range(0, len(need), 500):
                chunk = need[i:i + 500]
                end = self.end_ms if self.end_ms is not None else 1 << 62
                for t in chunk:
                    got = c.execute("SELECT at_ms, yes_bid, yes_ask, bid_size, ask_size, bid_depth, ask_depth, server_ts_ms, source FROM kalshi_top"
                                    " WHERE ticker = ? AND at_ms <= ? ORDER BY at_ms", (t, end)).fetchall()
                    if not got:
                        self._series[t] = None
                        continue
                    a = np.array([r[:-1] for r in got], dtype=np.float64)
                    self._series[t] = _Series(a[:, 0].astype(np.int64), {n: a[:, i + 1] for i, n in enumerate(KALSHI_COLS)}, [r[-1] or "" for r in got])
        finally:
            c.close()

    def read(self, position_ids: Iterable[str], at_ms: int | None = None) -> dict[str, tuple[MatchRow, KalshiQuote | None]]:
        t = self._t(at_ms)
        pids = list(position_ids)
        self._load_matches(pids)
        tickers = {m.kalshi_ticker for p in pids if (m := self._matches.get(p)) is not None and m.kalshi_ticker}
        self._load_series(tickers)
        out: dict[str, tuple[MatchRow, KalshiQuote | None]] = {}
        for p in pids:
            m = self._matches.get(p)
            if m is None:
                continue                               # no leg_matches row: absent, as kalshi_for_legs
            q = None
            s = self._series.get(m.kalshi_ticker) if m.kalshi_ticker else None
            if s is not None:
                i = s.idx(t)
                if i >= 0:
                    c = s.cols
                    st = _nan(c["server_ts_ms"][i])
                    q = KalshiQuote(m.kalshi_ticker, _nan(c["yes_bid"][i]), _nan(c["yes_ask"][i]), _nan(c["bid_size"][i]),
                                    _nan(c["ask_size"][i]), _nan(c["bid_depth"][i]), _nan(c["ask_depth"][i]), int(s.at[i]),
                                    None if st is None else int(st), s.labels[i] if s.labels else "")
            out[p] = (m, q)
        return out

    __call__ = read

    def confirm(self, at_ms: int | None = None) -> int | None:
        t = self._t(at_ms)
        if self._hb is None:
            c = ro(self.kalshi_db)
            try:
                if self.end_ms is None:
                    self._hb = Heartbeat.from_query(c, "SELECT DISTINCT at_ms FROM kalshi_top")
                else:
                    self._hb = Heartbeat.from_query(c, "SELECT DISTINCT at_ms FROM kalshi_top WHERE at_ms <= ?", self.end_ms)
            finally:
                c.close()
        return self._hb.at(t)

    def first_row_ms(self) -> int | None:
        c = ro(self.kalshi_db)
        try:
            r = c.execute("SELECT MIN(at_ms) FROM kalshi_top").fetchone()
            return None if r is None or r[0] is None else int(r[0])
        finally:
            c.close()


class CatalogRO:
    """``store.get_market(position_id)`` over rfq.db's markets table, read-only and cached (the only Store method the Assembler uses)."""

    def __init__(self, rfq_db: str | Path):
        self.conn = ro(rfq_db)
        self._cache: dict[str, sqlite3.Row | None] = {}

    def get_market(self, position_id: str) -> sqlite3.Row | None:
        if position_id not in self._cache:
            self._cache[position_id] = self.conn.execute("SELECT * FROM markets WHERE position_id=?", (position_id,)).fetchone()
        return self._cache[position_id]

    def close(self) -> None:
        self.conn.close()
