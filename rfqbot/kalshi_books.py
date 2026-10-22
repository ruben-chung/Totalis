"""Kalshi live price feed + persisted Polymarket-leg -> Kalshi-market matches.

Run as its own process, like ``rfqbot.books``:
    python -m rfqbot.kalshi_books                 # feed (sweep + depth polls + match refresh [+ websocket])
    python -m rfqbot.kalshi_books --stats         # coverage / freshness / match counts / request rate
    python -m rfqbot.kalshi_books --refresh-matches   # rebuild leg_matches once and exit

Why: the marginal layer blends a Polymarket quote and a Kalshi quote per leg. ``kalshi_markets``
(rfq.db, written by ``kalshi-sync``) is the catalog with parsed LegSpec fields; this module adds
prices and the persisted match of each Polymarket leg to a Kalshi ticker so neither the feed nor
the pricing layer recomputes matches.

Storage: ``data/kalshi_books.db`` (schema below, separate from rfq.db and books.db):
  * ``leg_matches``   one row per parsed Polymarket leg of every registered sport, from the shared
                      matcher (``sports.matcher.match``) over the latest ``kalshi_markets`` batch.
                      Rebuilt after each ``kalshi-sync`` (detected by ``MAX(synced_at_ms)`` changing)
                      and every ``KALSHI_MATCH_REFRESH_SEC``; a leg whose ticker / side / confidence
                      changes is rewritten, never silently kept.
  * ``kalshi_top``    change-only top-of-book rows, prices as YES probabilities in dollars (0..1),
                      sizes as contracts. ``source`` is ``batch`` (GET /markets?tickers=), ``orderbook``
                      (GET /markets/{t}/orderbook?depth=5) or ``ws`` (ticker channel). ``at_ms`` is
                      the local receive time and is the point-in-time key; ``server_ts_ms`` is the
                      venue timestamp where one exists (ws ``ts_ms``; batch ``updated_time`` is a
                      market-metadata time, NOT a quote time, stored for provenance only).
  * ``request_minutes`` request / 429 / error counts per minute for the rate report.
  ``leg_matches.kalshi_game_date`` (added 2026-10-21, migrated on open) is the resolved ET date of the
  Kalshi game; ``game_key`` keeps the Polymarket slug date (UTC for the NFL).
  * every REST response and websocket frame goes verbatim to a gz raw log (``KALSHI_LOG_DIR``).

Verified against the public API on 2026-10-20:
  * ``GET /markets/{ticker}/orderbook?depth=N`` returns ``{"orderbook_fp": {"yes_dollars": [[price,
    size], ...], "no_dollars": [...]}}`` with string decimals, ascending by price. Both arrays are
    resting BIDS (Kalshi's docs: "returns yes bids and no bids only"). Best yes bid = last
    ``yes_dollars`` level; best yes ask = 1 - best no bid. Checked on three markets against the
    market record: KXNBAGAME-26OCT21GSWLAL-GSW yes bids ..0.35, no bids ..0.64 -> record says
    yes_bid 0.35 / yes_ask 0.36. ``depth`` 1..100, 0 = all levels.
  * ``GET /markets?tickers=A,B,...&limit=N``: 200 tickers OK (URL ~6.5 kB); 300 tickers -> HTTP 414
    (URL too long), so the batch size is bounded by URL length, not by a documented maximum.
    Fields: ``yes_bid_dollars``, ``yes_ask_dollars``, ``yes_bid_size_fp``, ``yes_ask_size_fp``,
    ``last_price_dollars``, ``updated_time``; the legacy integer-cent fields are null.
  * Auth (docs.kalshi.com/getting_started/api_keys): headers ``KALSHI-ACCESS-KEY``,
    ``KALSHI-ACCESS-TIMESTAMP`` (unix ms) and ``KALSHI-ACCESS-SIGNATURE`` = base64 of the signature
    over ``timestamp + METHOD + path`` where path includes ``/trade-api/v2`` and excludes the query
    string. RSA keys: PSS, SHA-256, MGF1-SHA256, salt = digest length; Ed25519 keys sign directly.
    The websocket upgrade carries the same headers with path ``/trade-api/ws/v2``. Rate limits are
    token buckets per tier (Basic: 200 read tokens/s, 10 per request -> 20 req/s); 429 carries no
    Retry-After, so backoff is exponential. Unauthenticated limits are undocumented; bursts of
    ~1 req/s returned 429 earlier on 2026-10-20 (10 quick orderbook calls later passed).
  * Websocket ``ticker`` channel: ``{"id":1,"cmd":"subscribe","params":{"channels":["ticker"]}}``
    with no market filter streams every market; messages ``{"type":"ticker","msg":{"market_ticker",
    "yes_bid_dollars","yes_ask_dollars","yes_bid_size_fp","yes_ask_size_fp","price_dollars","ts_ms"}}``.
    Kalshi pings every 10 s; the websockets library answers automatically.
  * Quotes are fee-exclusive YES probabilities; taker fees are the pricing layer's business.

Never read the private key into logs, tests, fixtures or messages; ``KalshiAuth`` keeps only the
loaded key object and the key id.
"""
from __future__ import annotations

import asyncio
import atexit
import base64
import json
import logging
import sqlite3
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import httpx

from .db import SQLITE_IN_CHUNK, ro_connect
from .rawlog import RawLog
from .store import now_ms

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
DEFAULT_DB_PATH = Path("./data/kalshi_books.db")
BATCH_MAX_TICKERS = 200        # 300 -> HTTP 414 on 2026-10-20
BATCH_MAX_URL = 7_500          # characters of the tickers parameter; 6.5 kB passed, 9.5 kB failed
DEPTH_WINDOW = 0.05            # near-top depth: contracts within 0.05 of the best price
MIN_GAP_AUTH_S = 0.06          # seconds between requests with an API key (Basic tier: 20 read req/s)
MIN_GAP_UNAUTH_S = 1.1         # without a key; ~1 req/s bursts drew 429s on 2026-10-20
WS_MAX_FRAME = 16 * 1024 * 1024   # websocket frame cap (the unfiltered ticker channel sends large batches)
CONFIDENCES = ("exact", "rules_differ", "shifted_line", "none")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS leg_matches (
    position_id          TEXT PRIMARY KEY,
    sport                TEXT NOT NULL,
    canonical_key        TEXT NOT NULL,
    game_key             TEXT NOT NULL,
    kalshi_ticker        TEXT,                 -- NULL when confidence = none
    kalshi_side          TEXT,                 -- yes | no
    confidence           TEXT NOT NULL,        -- exact | rules_differ | shifted_line | none
    notes_json           TEXT NOT NULL,        -- list[str] settlement / reason notes from the matcher
    line_candidates_json TEXT,                 -- {"below": {...}, "above": {...}} for shifted_line
    matched_at_ms        INTEGER NOT NULL,
    kalshi_game_date     TEXT                  -- resolved ET game date of the Kalshi game (NULL when none found)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_leg_matches_ticker ON leg_matches(kalshi_ticker);
CREATE INDEX IF NOT EXISTS ix_leg_matches_game ON leg_matches(game_key);

-- one row per change of the top of book (not per response)
CREATE TABLE IF NOT EXISTS kalshi_top (
    ticker       TEXT NOT NULL,
    at_ms        INTEGER NOT NULL,     -- local receive time (point-in-time key)
    server_ts_ms INTEGER,              -- venue time where one exists (ws ts_ms); batch updated_time is metadata time
    yes_bid      REAL,                 -- YES probability, NULL if no bid
    yes_ask      REAL,                 -- = 1 - best NO bid, NULL if no NO bid
    bid_size     REAL,                 -- contracts at best
    ask_size     REAL,
    bid_depth    REAL,                 -- contracts within 0.05 of best (orderbook / ws snapshot only)
    ask_depth    REAL,
    source       TEXT NOT NULL         -- batch | orderbook | ws
);
CREATE INDEX IF NOT EXISTS ix_kalshi_top_ticker_time ON kalshi_top(ticker, at_ms);
CREATE INDEX IF NOT EXISTS ix_kalshi_top_time ON kalshi_top(at_ms);

-- last time each ticker was observed (any source); kalshi_top is change-only, so this is the freshness key
CREATE TABLE IF NOT EXISTS ticker_seen (
    ticker       TEXT PRIMARY KEY,
    last_seen_ms INTEGER NOT NULL,
    source       TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS request_minutes (
    minute_ms  INTEGER NOT NULL,
    kind       TEXT NOT NULL,            -- batch | orderbook | ws
    requests   INTEGER NOT NULL DEFAULT 0,
    n429       INTEGER NOT NULL DEFAULT 0,
    errors     INTEGER NOT NULL DEFAULT 0,
    tickers    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (minute_ms, kind)
) WITHOUT ROWID;
"""


# ----------------------------------------------------------------------- types
@dataclass(frozen=True)
class KalshiQuote:
    ticker: str
    yes_bid: float | None
    yes_ask: float | None
    bid_size: float | None
    ask_size: float | None
    bid_depth: float | None
    ask_depth: float | None
    at_ms: int
    server_ts_ms: int | None
    source: str = ""

    @property
    def mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2


@dataclass(frozen=True)
class MatchRow:
    position_id: str
    sport: str
    canonical_key: str
    game_key: str
    kalshi_ticker: str | None
    kalshi_side: str | None
    confidence: str
    notes: list[str] = field(default_factory=list)
    line_candidates: dict[str, Any] | None = None
    matched_at_ms: int = 0
    kalshi_game_date: str | None = None        # resolved ET date of the Kalshi game (the Poly game_key keeps the slug date)

    @classmethod
    def from_row(cls, r: sqlite3.Row | dict) -> "MatchRow":
        keys = r.keys() if hasattr(r, "keys") else r
        return cls(position_id=r["position_id"], sport=r["sport"], canonical_key=r["canonical_key"], game_key=r["game_key"],
                   kalshi_ticker=r["kalshi_ticker"], kalshi_side=r["kalshi_side"], confidence=r["confidence"],
                   notes=json.loads(r["notes_json"] or "[]"),
                   line_candidates=json.loads(r["line_candidates_json"]) if r["line_candidates_json"] else None,
                   matched_at_ms=r["matched_at_ms"], kalshi_game_date=r["kalshi_game_date"] if "kalshi_game_date" in keys else None)


Top = tuple[float | None, float | None, float | None, float | None, float | None, float | None]


# --------------------------------------------------------------------- parsing
def _f(x) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _iso_ms(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


def parse_orderbook(body: dict[str, Any]) -> Top:
    """``orderbook_fp`` -> (yes_bid, yes_ask, bid_size, ask_size, bid_depth, ask_depth).

    Both arrays are bids: yes_dollars are YES bids, no_dollars are NO bids. A NO bid at p is a YES
    offer at 1 - p, so yes_ask = 1 - max(no price) and ask_size is the size at that NO level.
    Depth = contracts within DEPTH_WINDOW of the best price on that side (in YES-price terms the
    ask depth is the NO bids within 0.05 of the best NO bid)."""
    fp = body.get("orderbook_fp") or body.get("orderbook") or {}
    yes = [(float(p), float(s)) for p, s in (fp.get("yes_dollars") or fp.get("yes") or []) if float(s) > 0]
    no = [(float(p), float(s)) for p, s in (fp.get("no_dollars") or fp.get("no") or []) if float(s) > 0]
    yes_bid = yes_ask = bid_size = ask_size = bid_depth = ask_depth = None
    if yes:
        yb = max(p for p, _ in yes)
        yes_bid, bid_size = yb, sum(s for p, s in yes if p == yb)
        bid_depth = sum(s for p, s in yes if p >= yb - DEPTH_WINDOW - 1e-9)
    if no:
        nb = max(p for p, _ in no)
        yes_ask, ask_size = round(1.0 - nb, 4), sum(s for p, s in no if p == nb)
        ask_depth = sum(s for p, s in no if p >= nb - DEPTH_WINDOW - 1e-9)
    return (yes_bid, yes_ask, bid_size, ask_size, bid_depth, ask_depth)


def parse_batch_market(m: dict[str, Any]) -> tuple[Top, int | None]:
    """One GET /markets record -> (top, server_ts_ms). Uses the *_dollars / *_fp fields only (the
    integer-cent ones are null). Depth is unknown from this endpoint (None)."""
    yes_bid, yes_ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    bid_size, ask_size = _f(m.get("yes_bid_size_fp")), _f(m.get("yes_ask_size_fp"))
    if yes_bid is not None and yes_bid <= 0 and not bid_size:
        yes_bid, bid_size = None, None       # Kalshi reports 0.00 for an empty bid side
    if yes_ask is not None and yes_ask >= 1 and not ask_size:
        yes_ask, ask_size = None, None       # and 1.00 for an empty ask side
    return (yes_bid, yes_ask, bid_size, ask_size, None, None), _iso_ms(m.get("updated_time"))


BATCH_LOG_FIELDS = ("ticker", "event_ticker", "status", "yes_bid_dollars", "yes_ask_dollars", "no_bid_dollars", "no_ask_dollars",
                    "yes_bid_size_fp", "yes_ask_size_fp", "last_price_dollars", "updated_time", "close_time")


def trim_batch_response(text: str, keep: set[str] | None = None) -> str:
    """Raw-log form of a GET /markets response: only BATCH_LOG_FIELDS per market and, when ``keep``
    is given, only the markets in it (the sweep passes the tickers whose top changed; unchanged
    repeats are redundant with kalshi_top + ticker_seen). ``n_markets`` records the full count.
    Unparseable text is returned unchanged so nothing is lost."""
    try:
        body = json.loads(text)
        markets = body.get("markets")
        if not isinstance(markets, list):
            return text
    except (ValueError, AttributeError):
        return text
    rows = [m for m in markets if isinstance(m, dict) and (keep is None or m.get("ticker") in keep)]
    slim = {"n_markets": len(markets), "markets": [{k: m.get(k) for k in BATCH_LOG_FIELDS if k in m} for m in rows]}
    if body.get("cursor"):
        slim["cursor"] = body["cursor"]
    return json.dumps(slim, separators=(",", ":"))


def parse_ws_ticker(msg: dict[str, Any]) -> tuple[str, Top, int | None]:
    """A ``ticker`` channel message body -> (ticker, top, ts_ms)."""
    top, _ = parse_batch_market(msg)
    ts = msg.get("ts_ms")
    if ts is None and msg.get("ts") is not None:
        ts = int(msg["ts"]) * 1000
    return str(msg.get("market_ticker", "")), top, (int(ts) if ts is not None else None)


# ------------------------------------------------------------------------ auth
class KalshiAuth:
    """Signs requests with the API key. Loads the PEM once; the key material is never logged."""

    def __init__(self, key_id: str, private_key_path: str | Path):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

        self.key_id = key_id
        pem = Path(private_key_path).expanduser().read_bytes()
        self._key = serialization.load_pem_private_key(pem, password=None)
        del pem
        if isinstance(self._key, rsa.RSAPrivateKey):
            self.kind = "rsa"
        elif isinstance(self._key, ed25519.Ed25519PrivateKey):
            self.kind = "ed25519"
        else:
            raise ValueError("unsupported Kalshi private key type (need RSA or Ed25519)")

    @classmethod
    def from_settings(cls, settings) -> "KalshiAuth | None":
        key_id = getattr(settings, "kalshi_key_id", "")
        path = getattr(settings, "kalshi_private_key_path", None)
        if not key_id or not path:
            return None
        if not Path(path).expanduser().exists():
            log.warning("KALSHI_PRIVATE_KEY_PATH does not exist; running unauthenticated")
            return None
        return cls(key_id, path)

    def sign(self, message: bytes) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        if self.kind == "rsa":
            sig = self._key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                                 hashes.SHA256())
        else:
            sig = self._key.sign(message)
        return base64.b64encode(sig).decode("ascii")

    def headers(self, method: str, path: str, *, timestamp_ms: int | None = None) -> dict[str, str]:
        """``path`` must include the API prefix (/trade-api/v2/... or /trade-api/ws/v2) and no query string."""
        path = path.split("?", 1)[0]
        ts = str(timestamp_ms if timestamp_ms is not None else now_ms())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": self.sign((ts + method.upper() + path).encode("utf-8"))}

    def __repr__(self) -> str:  # never show key material
        return f"KalshiAuth(key_id=...{self.key_id[-4:]}, kind={self.kind})"


# ---------------------------------------------------------------------- client
class KalshiFeedClient:
    """REST client for the feed: optional auth, min gap, exponential backoff on 429 / 5xx, counters."""

    def __init__(self, base_url: str = DEFAULT_API_URL, *, auth: KalshiAuth | None = None,
                 client: httpx.AsyncClient | None = None, min_gap_sec: float | None = None, max_retries: int = 5,
                 on_response: Callable[[str, str, int], None] | None = None):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self._client = client
        self._own = client is None
        self.min_gap_sec = (MIN_GAP_AUTH_S if auth else MIN_GAP_UNAUTH_S) if min_gap_sec is None else min_gap_sec
        self.max_retries = max_retries
        self.on_response = on_response          # (kind, text, at_ms) -> raw log
        self._last_request = 0.0
        self.counts: Counter = Counter()        # requests, n429, errors, by kind
        self._stamps: deque[float] = deque(maxlen=2_000)
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "KalshiFeedClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30, headers={"User-Agent": "rfqbot/0.1 kalshi_books"})
        return self

    async def __aexit__(self, *exc) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    def rate_per_sec(self, window_sec: float = 60.0) -> float:
        now = time.monotonic()
        n = sum(1 for t in self._stamps if now - t <= window_sec)
        return n / window_sec

    def _path(self, path: str) -> str:
        """Signing path: the URL path including /trade-api/v2."""
        return httpx.URL(self.base_url + path).path

    async def get(self, path: str, params: dict[str, Any] | None = None, *, kind: str = "get") -> dict[str, Any]:
        assert self._client is not None, "use 'async with KalshiFeedClient()'"
        async with self._lock:
            for attempt in range(self.max_retries + 1):
                gap = self.min_gap_sec - (time.monotonic() - self._last_request)
                if gap > 0:
                    await asyncio.sleep(gap)
                self._last_request = time.monotonic()
                self._stamps.append(self._last_request)
                self.counts["requests"] += 1
                self.counts[f"requests_{kind}"] += 1
                headers = self.auth.headers("GET", self._path(path)) if self.auth else None
                try:
                    r = await self._client.get(f"{self.base_url}{path}", params=params, headers=headers)
                except httpx.HTTPError as e:
                    self.counts["errors"] += 1
                    if attempt == self.max_retries:
                        raise
                    log.warning("kalshi %s: %s (retry %d)", path, e, attempt + 1)
                    await asyncio.sleep(min(30, 1.5 * 2 ** attempt))
                    continue
                at = now_ms()
                if self.on_response is not None:
                    try:
                        self.on_response(kind, r.text, at)
                    except Exception as e:  # raw log must never stop the feed
                        log.warning("raw log write failed: %s", e)
                if r.status_code == 429 or r.status_code >= 500:
                    self.counts["n429" if r.status_code == 429 else "errors"] += 1
                    if attempt == self.max_retries:
                        r.raise_for_status()
                    wait = min(30.0, 1.5 * 2 ** attempt)
                    log.warning("kalshi %s: HTTP %d, backing off %.1fs (rate %.2f/s)", path, r.status_code, wait, self.rate_per_sec())
                    await asyncio.sleep(wait)
                    continue
                if r.status_code >= 400:
                    self.counts["errors"] += 1
                    r.raise_for_status()
                return r.json()
        raise RuntimeError("unreachable")

    @staticmethod
    def chunk_tickers(tickers: Sequence[str], max_n: int = BATCH_MAX_TICKERS, max_chars: int = BATCH_MAX_URL) -> list[list[str]]:
        chunks: list[list[str]] = []
        cur: list[str] = []
        size = 0
        for t in tickers:
            if cur and (len(cur) >= max_n or size + len(t) + 1 > max_chars):
                chunks.append(cur); cur, size = [], 0
            cur.append(t); size += len(t) + 1
        if cur:
            chunks.append(cur)
        return chunks

    async def batch_markets(self, tickers: Sequence[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for chunk in self.chunk_tickers(list(tickers)):
            body = await self.get("/markets", {"tickers": ",".join(chunk), "limit": len(chunk)}, kind="batch")
            out.extend(body.get("markets") or [])
        return out

    async def orderbook(self, ticker: str, depth: int = 5) -> dict[str, Any]:
        return await self.get(f"/markets/{ticker}/orderbook", {"depth": depth}, kind="orderbook")


# ----------------------------------------------------------------------- store
class KalshiBookStore:
    """Writer (the feed) or read-only reader (``readonly=True``: ``rfqbot.db.ro_connect``, so a missing file
    raises instead of being created). The writer's connection is used from the event-loop thread only;
    the matcher's catalog work runs in a worker thread with its own read-only connections and hands
    the rows back to the loop thread for the upsert (``compute_leg_matches`` / ``apply_leg_matches``)."""

    def __init__(self, path: str | Path | None, *, readonly: bool = False):
        self.path = ":memory:" if path is None else str(path)
        self.readonly = readonly and self.path != ":memory:"
        if self.readonly:
            self.conn = ro_connect(self.path, timeout=5, rows=True, shared=True)
        else:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout=5000")
            if self.path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            self._migrate()
        self._in_tx = False
        self.last_top: dict[str, Top] = {}      # newest (yes_bid, yes_ask, bid_size, ask_size, bid_depth, ask_depth) per ticker
        if not self.readonly:
            self._load_last_tops()

    def _migrate(self) -> None:
        """Add columns introduced after the first schema (databases created by an older feed)."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(leg_matches)")}
        if "kalshi_game_date" not in cols:
            self.conn.execute("ALTER TABLE leg_matches ADD COLUMN kalshi_game_date TEXT")

    def _load_last_tops(self) -> None:
        """Seed the change-only cache from the newest row per ticker so a restart does not duplicate rows."""
        for r in self.conn.execute("SELECT t.ticker, t.yes_bid, t.yes_ask, t.bid_size, t.ask_size, t.bid_depth, t.ask_depth FROM kalshi_top t"
                                   " JOIN (SELECT ticker, MAX(at_ms) AS m FROM kalshi_top GROUP BY ticker) x"
                                   " ON x.ticker = t.ticker AND x.m = t.at_ms"):
            self.last_top[r["ticker"]] = (r["yes_bid"], r["yes_ask"], r["bid_size"], r["ask_size"], r["bid_depth"], r["ask_depth"])

    def begin(self) -> None:
        if not self._in_tx:
            self.conn.execute("BEGIN"); self._in_tx = True

    def commit(self) -> None:
        if self._in_tx:
            self.conn.execute("COMMIT"); self._in_tx = False

    def close(self) -> None:
        if not self.readonly:
            self.commit()
        self.conn.close()

    def scalar(self, sql: str, *args):
        r = self.conn.execute(sql, args).fetchone()
        return None if r is None else r[0]

    # -- quotes ---------------------------------------------------------------
    def insert_top(self, ticker: str, at_ms: int, server_ts_ms: int | None, top: Top, source: str, *,
                   force: bool = False) -> bool:
        """Write a row only when best bid / ask price or size changed (or ``force``). Returns True if written.
        A row without depth (batch / ws) whose best prices equal the previous row's inherits that row's
        depth, so ``latest_quote`` keeps the last orderbook depth while the top of book is unchanged."""
        prev = self.last_top.get(ticker)
        key = top[:4]
        if not force and prev is not None and prev[:4] == key:
            return False
        if top[4] is None and top[5] is None and prev is not None and (prev[4] is not None or prev[5] is not None) \
                and prev[0] == top[0] and prev[1] == top[1]:
            top = (*key, prev[4], prev[5])
        self.last_top[ticker] = top
        self.conn.execute("INSERT INTO kalshi_top VALUES (?,?,?,?,?,?,?,?,?,?)", (ticker, at_ms, server_ts_ms, *top, source))
        return True

    def mark_seen(self, tickers: Iterable[str], at_ms: int, source: str) -> None:
        self.conn.executemany("INSERT INTO ticker_seen(ticker, last_seen_ms, source) VALUES (?,?,?)"
                              " ON CONFLICT(ticker) DO UPDATE SET last_seen_ms=excluded.last_seen_ms, source=excluded.source",
                              [(t, at_ms, source) for t in tickers])

    def seen_since(self, since_ms: int) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT ticker FROM ticker_seen WHERE last_seen_ms >= ?", (since_ms,))}

    def latest_quote(self, ticker: str, at_ms: int | None = None) -> KalshiQuote | None:
        if at_ms is None:
            r = self.conn.execute("SELECT * FROM kalshi_top WHERE ticker=? ORDER BY at_ms DESC LIMIT 1", (ticker,)).fetchone()
        else:
            r = self.conn.execute("SELECT * FROM kalshi_top WHERE ticker=? AND at_ms<=? ORDER BY at_ms DESC LIMIT 1",
                                  (ticker, at_ms)).fetchone()
        return None if r is None else KalshiQuote(r["ticker"], r["yes_bid"], r["yes_ask"], r["bid_size"], r["ask_size"],
                                                  r["bid_depth"], r["ask_depth"], r["at_ms"], r["server_ts_ms"], r["source"])

    def quotes(self, tickers: Iterable[str], at_ms: int | None = None) -> dict[str, KalshiQuote]:
        out = {}
        for t in set(tickers):
            q = self.latest_quote(t, at_ms)
            if q is not None:
                out[t] = q
        return out

    # -- matches --------------------------------------------------------------
    def upsert_matches(self, rows: Iterable[dict[str, Any]]) -> Counter:
        """Insert / rewrite leg_matches rows. Returns Counter(new, changed, unchanged)."""
        c: Counter = Counter()
        for r in rows:
            r = {"kalshi_game_date": None, **r}
            old = self.conn.execute("SELECT kalshi_ticker, kalshi_side, confidence, canonical_key, kalshi_game_date FROM leg_matches"
                                    " WHERE position_id=?", (r["position_id"],)).fetchone()
            if old is None:
                c["new"] += 1
            elif (old["kalshi_ticker"], old["kalshi_side"], old["confidence"], old["canonical_key"], old["kalshi_game_date"]) == \
                    (r["kalshi_ticker"], r["kalshi_side"], r["confidence"], r["canonical_key"], r["kalshi_game_date"]):
                c["unchanged"] += 1
                self.conn.execute("UPDATE leg_matches SET matched_at_ms=?, notes_json=?, line_candidates_json=? WHERE position_id=?",
                                  (r["matched_at_ms"], r["notes_json"], r["line_candidates_json"], r["position_id"]))
                continue
            else:
                c["changed"] += 1
            self.conn.execute(
                "INSERT INTO leg_matches(position_id, sport, canonical_key, game_key, kalshi_ticker, kalshi_side, confidence,"
                " notes_json, line_candidates_json, matched_at_ms, kalshi_game_date) VALUES (:position_id, :sport, :canonical_key,"
                " :game_key, :kalshi_ticker, :kalshi_side, :confidence, :notes_json, :line_candidates_json, :matched_at_ms, :kalshi_game_date)"
                " ON CONFLICT(position_id) DO UPDATE SET sport=excluded.sport, canonical_key=excluded.canonical_key,"
                " game_key=excluded.game_key, kalshi_ticker=excluded.kalshi_ticker, kalshi_side=excluded.kalshi_side,"
                " confidence=excluded.confidence, notes_json=excluded.notes_json, kalshi_game_date=excluded.kalshi_game_date,"
                " line_candidates_json=excluded.line_candidates_json, matched_at_ms=excluded.matched_at_ms", r)
        return c

    def matches(self, position_ids: Iterable[str]) -> dict[str, MatchRow]:
        out: dict[str, MatchRow] = {}
        ids = list(set(position_ids))
        for i in range(0, len(ids), SQLITE_IN_CHUNK):
            chunk = ids[i:i + SQLITE_IN_CHUNK]
            for r in self.conn.execute(f"SELECT * FROM leg_matches WHERE position_id IN ({','.join('?' * len(chunk))})", chunk):
                out[r["position_id"]] = MatchRow.from_row(r)
        return out

    def matched_tickers(self, position_ids: Iterable[str] | None = None) -> set[str]:
        if position_ids is None:
            return {r[0] for r in self.conn.execute("SELECT DISTINCT kalshi_ticker FROM leg_matches WHERE kalshi_ticker IS NOT NULL")}
        return {m.kalshi_ticker for m in self.matches(position_ids).values() if m.kalshi_ticker}

    def match_counts(self) -> Counter:
        return Counter({r[0]: r[1] for r in self.conn.execute("SELECT confidence, COUNT(*) FROM leg_matches GROUP BY confidence")})

    # -- request stats ----------------------------------------------------------
    def bump_minute(self, at_ms: int, kind: str, *, requests: int = 0, n429: int = 0, errors: int = 0, tickers: int = 0) -> None:
        minute = at_ms - at_ms % 60_000
        self.conn.execute("INSERT INTO request_minutes(minute_ms, kind, requests, n429, errors, tickers) VALUES (?,?,?,?,?,?)"
                          " ON CONFLICT(minute_ms, kind) DO UPDATE SET requests=requests+excluded.requests, n429=n429+excluded.n429,"
                          " errors=errors+excluded.errors, tickers=tickers+excluded.tickers",
                          (minute, kind, requests, n429, errors, tickers))

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, json.dumps(value)))

    def get_meta(self, key: str, default: Any = None) -> Any:
        v = self.scalar("SELECT value FROM meta WHERE key=?", key)
        return default if v is None else json.loads(v)


# ------------------------------------------------------------------ matching
def _rfq_conn(rfq_db: str | Path) -> sqlite3.Connection:
    return ro_connect(rfq_db, timeout=5)


def kalshi_sync_stamp(rfq_db: str | Path) -> int:
    """MAX(synced_at_ms) of kalshi_markets: changes exactly when kalshi-sync writes."""
    try:
        c = _rfq_conn(rfq_db)
        try:
            return int(c.execute("SELECT COALESCE(MAX(synced_at_ms), 0) FROM kalshi_markets").fetchone()[0])
        finally:
            c.close()
    except sqlite3.Error:
        return 0


def load_kalshi_index(rfq_db: str | Path):
    """KalshiIndex over the latest synced batch per sport (parse_ok rows only)."""
    from .kalshi import spec_from_row
    from .sports import registry  # noqa: F401  (registers sports)
    from .sports.matcher import KalshiIndex

    idx = KalshiIndex()
    c = _rfq_conn(rfq_db)
    try:
        latest = {r[0]: r[1] for r in c.execute("SELECT sport, MAX(synced_at_ms) FROM kalshi_markets GROUP BY sport")}
        for sport, stamp in latest.items():
            for r in c.execute("SELECT * FROM kalshi_markets WHERE sport=? AND synced_at_ms=? AND parse_ok=1", (sport, stamp)):
                try:
                    idx.add(spec_from_row(r))
                except ValueError as e:
                    log.warning("kalshi row %s no longer parses: %s", r["ticker"], e)
    finally:
        c.close()
    return idx


def load_poly_legs(rfq_db: str | Path, sports: Iterable[str] | None = None) -> tuple[list, Counter]:
    """Parsed LegSpecs for every catalog position of the registered sports (or ``sports``)."""
    from .sports import registry

    specs, failed = [], Counter()
    c = _rfq_conn(rfq_db)
    try:
        for sport in (sports or registry.sports()):
            sp = registry.get(sport)
            for r in c.execute("SELECT position_id, slug, title, outcome, outcome_index FROM markets WHERE slug LIKE ?",
                               (sp.slug_prefix + "%",)):
                try:
                    specs.append(sp.parse_poly(r["slug"], r["title"], r["outcome"], r["outcome_index"], position_id=r["position_id"]))
                except ValueError:
                    failed[sport] += 1
    finally:
        c.close()
    return specs, failed


AMBIGUOUS_NOTE = "ambiguous_game"


def match_rows(specs: Iterable, idx, now: int) -> list[dict[str, Any]]:
    """One leg_matches row per spec. A leg whose team pair has two Kalshi games inside the date tolerance
    (``AmbiguousGame``) gets confidence ``none`` with an ``ambiguous_game`` note instead of aborting the
    whole rebuild."""
    from .sports.legspec import canonical_key
    from .sports.matcher import AmbiguousGame, MatchResult, match

    rows = []
    for s in specs:
        try:
            m = match(s, idx)
        except AmbiguousGame as e:
            m = MatchResult("none", notes=[f"{AMBIGUOUS_NOTE}: {e}"])
        cands = None
        if m.below or m.above:
            cands = {k: {"ticker": c.kalshi_ticker, "side": c.kalshi_side, "line": c.line}
                     for k, c in (("below", m.below), ("above", m.above)) if c}
        rows.append({"position_id": s.venue_ids.get("position_id") or s.venue_ids.get("slug"), "sport": s.sport,
                     "canonical_key": canonical_key(s), "game_key": s.game_key, "kalshi_ticker": m.kalshi_ticker,
                     "kalshi_side": m.kalshi_side, "confidence": m.confidence, "notes_json": json.dumps(m.notes),
                     "line_candidates_json": json.dumps(cands) if cands else None, "matched_at_ms": now,
                     "kalshi_game_date": m.game_date})
    return rows


def compute_leg_matches(rfq_db: str | Path, now: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The read side of a refresh (catalog + kalshi_markets -> rows); safe to run in a worker thread,
    it opens its own read-only connections and touches no store."""
    now = now or now_ms()
    t0 = time.monotonic()
    idx = load_kalshi_index(rfq_db)
    specs, failed = load_poly_legs(rfq_db)
    rows = match_rows(specs, idx, now)
    info = {"now": now, "kalshi_markets": idx.n, "parse_failures": dict(failed), "sync_stamp": kalshi_sync_stamp(rfq_db),
            "ambiguous": sum(1 for r in rows if r["confidence"] == "none" and AMBIGUOUS_NOTE in (r["notes_json"] or "")),
            "compute_s": time.monotonic() - t0}
    return rows, info


def apply_leg_matches(store: KalshiBookStore, rows: list[dict[str, Any]], info: dict[str, Any]) -> dict[str, Any]:
    """The write side of a refresh: one transaction on the store's connection (call on its owning thread)."""
    store.begin()
    changes = store.upsert_matches(rows)
    store.set_meta("matches_refreshed_ms", info["now"])
    store.set_meta("matches_sync_stamp", info["sync_stamp"])
    store.commit()
    conf = Counter(r["confidence"] for r in rows)
    log.info("leg_matches: %d legs (%s) over %d kalshi markets in %.1fs; new %d changed %d unchanged %d; ambiguous %d; poly parse failures %s",
             len(rows), ", ".join(f"{k}={v}" for k, v in sorted(conf.items())), info["kalshi_markets"], info["compute_s"],
             changes["new"], changes["changed"], changes["unchanged"], info["ambiguous"], info["parse_failures"] or "none")
    return {"legs": len(rows), "kalshi_markets": info["kalshi_markets"], "confidence": dict(conf), "changes": dict(changes),
            "parse_failures": info["parse_failures"], "ambiguous": info["ambiguous"]}


def refresh_leg_matches(store: KalshiBookStore, rfq_db: str | Path, *, now: int | None = None) -> dict[str, Any]:
    """Rebuild leg_matches from the current catalog + kalshi_markets (compute + apply on this thread). Returns counts."""
    rows, info = compute_leg_matches(rfq_db, now)
    return apply_leg_matches(store, rows, info)


# --------------------------------------------------------------------- universe
def open_kalshi_tickers(rfq_db: str | Path) -> list[str]:
    """Every open game-level kalshi_markets ticker for registered sports (latest batch per sport)."""
    from .sports import registry

    c = _rfq_conn(rfq_db)
    try:
        sports = registry.sports()
        out = []
        for sport in sports:
            out += [r[0] for r in c.execute(
                "SELECT ticker FROM kalshi_markets WHERE sport=? AND status IN ('active','open','initialized')"
                " AND synced_at_ms = (SELECT MAX(synced_at_ms) FROM kalshi_markets WHERE sport=?) ORDER BY ticker", (sport, sport))]
        return out
    finally:
        c.close()


# ------------------------------------------------------------------------- feed
class KalshiFeed:
    """Sweep (batch) + depth polls (orderbook) + match refresh [+ websocket ticker stream]."""

    def __init__(self, store: KalshiBookStore, client: KalshiFeedClient, rfq_db: str | Path, *,
                 sweep_sec: float = 15.0, depth_sec: float = 30.0, depth_levels: int = 5, match_refresh_sec: float = 600.0,
                 universe_hours: float = 6.0, rawlog: RawLog | None = None, ws_url: str | None = None,
                 universe: Callable[[], list[str]] | None = None, depth_targets: Callable[[], set[str]] | None = None):
        self.store = store
        self.client = client
        self.rfq_db = rfq_db
        self.sweep_sec = sweep_sec
        self.depth_sec = depth_sec
        self.depth_levels = depth_levels
        self.match_refresh_sec = match_refresh_sec
        self.rawlog = rawlog
        self.ws_url = ws_url
        self._universe_fn = universe or (lambda: open_kalshi_tickers(rfq_db))
        self._depth_targets_fn = depth_targets
        self._universe: list[str] = []
        self._universe_set: frozenset[str] = frozenset()
        self.counts: Counter = Counter()
        self.stopping = False
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._ws_tickers_seen = 0
        self._legs = None
        if depth_targets is None:
            from .books import RfqLegUniverse
            try:
                self._legs = RfqLegUniverse(rfq_db, window_ms=int(universe_hours * 3_600_000))
            except sqlite3.Error as e:
                log.warning("no RFQ leg universe (%s): depth polls cover every matched ticker", e)
        client.on_response = self._on_response

    @property
    def universe(self) -> list[str]:
        return self._universe

    @universe.setter
    def universe(self, tickers: Iterable[str]) -> None:
        """The membership set used by the websocket filter is rebuilt on every assignment."""
        self._universe = list(tickers)
        self._universe_set = frozenset(self._universe)

    # -- raw log --------------------------------------------------------------
    def _on_response(self, kind: str, text: str, at_ms: int) -> None:
        """Orderbook and websocket frames are logged verbatim. Batch responses carry full market
        records (rules text etc., ~3.8 GB/day at 15 s sweeps; 1.4 GB/day trimmed to quote fields)
        and are logged by ``sweep_once`` instead: quote fields only, changed markets only."""
        if self.rawlog is None or kind == "batch":
            return
        self.rawlog.write(at_ms=at_ms, direction="in", conn_id=kind, seq=None, text=text)

    # -- lifecycle ------------------------------------------------------------
    def stop(self) -> None:
        self.stopping = True
        self._stop.set()

    async def _sleep(self, sec: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=sec)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        async with self.client:
            self._tasks = [asyncio.create_task(self._matcher(), name="kalshi-matcher"),
                           asyncio.create_task(self._sweeper(), name="kalshi-sweeper"),
                           asyncio.create_task(self._depth_poller(), name="kalshi-depth")]
            if self.ws_url and self.client.auth is not None:
                self._tasks.append(asyncio.create_task(self._ws_loop(), name="kalshi-ws"))
            try:
                await self._stop.wait()
            finally:
                self.stopping = True
                for t in self._tasks:
                    t.cancel()
                await asyncio.gather(*self._tasks, return_exceptions=True)
                self.store.commit()
                if self.rawlog is not None:
                    self.rawlog.close()

    # -- loops ----------------------------------------------------------------
    async def _matcher(self) -> None:
        last_stamp = self.store.get_meta("matches_sync_stamp", -1)
        last_run = self.store.get_meta("matches_refreshed_ms", 0)
        while not self.stopping:
            stamp = kalshi_sync_stamp(self.rfq_db)
            due = stamp != last_stamp or now_ms() - last_run >= self.match_refresh_sec * 1000
            if due:
                try:
                    # the catalog read + matching runs off the loop; the upsert runs here, on the thread that
                    # owns the store connection, so its transaction never interleaves with the sweeper's
                    rows, info = await asyncio.to_thread(compute_leg_matches, self.rfq_db)
                    apply_leg_matches(self.store, rows, info)
                    last_stamp, last_run = stamp, now_ms()
                    self.counts["match_refreshes"] += 1
                except Exception as e:  # rfq.db may be mid-write; try again next tick
                    log.warning("leg_matches refresh failed: %s", e)
                    self.store.commit()
            await self._sleep(5.0)

    async def _sweeper(self) -> None:
        while not self.stopping:
            t0 = time.monotonic()
            try:
                self.universe = self._universe_fn()
                if self.universe:
                    await self.sweep_once(self.universe)
            except Exception as e:
                log.warning("sweep failed: %s", e)
                self.store.commit()
            await self._sleep(max(0.5, self.sweep_sec - (time.monotonic() - t0)))

    async def sweep_once(self, tickers: Sequence[str]) -> int:
        """Batch top-of-book over ``tickers``; returns rows written."""
        written = 0
        for chunk in self.client.chunk_tickers(list(tickers)):
            c0 = self.client.counts["n429"]
            try:
                markets = await self.client.batch_markets(chunk)
            except Exception as e:
                self.store.bump_minute(now_ms(), "batch", requests=1, errors=1, n429=self.client.counts["n429"] - c0)
                log.warning("batch of %d failed: %s", len(chunk), e)
                continue
            at = now_ms()
            self.store.begin()
            changed: set[str] = set()
            for m in markets:
                top, server_ts = parse_batch_market(m)
                if self.store.insert_top(m["ticker"], at, server_ts, top, "batch"):
                    written += 1
                    changed.add(m["ticker"])
            self.store.mark_seen([m["ticker"] for m in markets], at, "batch")
            if self.rawlog is not None:
                self.rawlog.write(at_ms=at, direction="in", conn_id="batch", seq=None,
                                  text=trim_batch_response(json.dumps({"markets": markets}), changed))
            self.store.bump_minute(at, "batch", requests=1, n429=self.client.counts["n429"] - c0, tickers=len(markets))
            self.store.commit()
            self.counts["batch_markets"] += len(markets)
        self.counts["sweeps"] += 1
        self.counts["top_rows"] += written
        return written

    def depth_targets(self) -> set[str]:
        if self._depth_targets_fn is not None:
            return set(self._depth_targets_fn())
        if self._legs is None:
            return self.store.matched_tickers()
        try:
            legs = self._legs()
        except sqlite3.Error as e:
            log.warning("rfq leg universe read failed: %s", e)
            return set()
        return self.store.matched_tickers(legs)

    async def _depth_poller(self) -> None:
        await self._sleep(2.0)
        while not self.stopping:
            t0 = time.monotonic()
            try:
                targets = sorted(self.depth_targets())
                self.counts["depth_targets"] = len(targets)
                await self.depth_once(targets)
            except Exception as e:
                log.warning("depth poll failed: %s", e)
                self.store.commit()
            await self._sleep(max(0.5, self.depth_sec - (time.monotonic() - t0)))

    async def depth_once(self, tickers: Sequence[str]) -> int:
        written = 0
        for t in tickers:
            if self.stopping:
                break
            c0 = self.client.counts["n429"]
            try:
                body = await self.client.orderbook(t, self.depth_levels)
            except Exception as e:
                self.store.bump_minute(now_ms(), "orderbook", requests=1, errors=1, n429=self.client.counts["n429"] - c0)
                log.warning("orderbook %s failed: %s", t, e)
                continue
            at = now_ms()
            top = parse_orderbook(body)
            self.store.begin()
            if self.store.insert_top(t, at, None, top, "orderbook", force=True):   # depth rows always carry new depth info
                written += 1
            self.store.mark_seen([t], at, "orderbook")
            self.store.bump_minute(at, "orderbook", requests=1, n429=self.client.counts["n429"] - c0, tickers=1)
            self.store.commit()
        self.counts["depth_polls"] += len(tickers)
        self.counts["top_rows"] += written
        return written

    # -- websocket (ticker channel, all markets) ------------------------------
    async def _ws_loop(self) -> None:
        import websockets
        from websockets.asyncio.client import connect

        backoff = 0
        while not self.stopping:
            headers = self.client.auth.headers("GET", httpx.URL(self.ws_url).path) if self.client.auth else {}
            got = False
            try:
                async with connect(self.ws_url, additional_headers=headers, max_size=WS_MAX_FRAME) as ws:
                    await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker"]}}))
                    log.info("kalshi ws connected, ticker channel subscribed")
                    while not self.stopping:
                        text = await ws.recv()
                        if isinstance(text, bytes):
                            text = text.decode("utf-8", "replace")
                        got = True; backoff = 0
                        self.on_ws_frame(text, now_ms())
            except (OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError) as e:
                log.warning("kalshi ws: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("kalshi ws unexpected error")
            if self.stopping:
                break
            backoff = 0 if got else min(backoff + 1, 6)
            await self._sleep(min(60.0, 2.0 * 2 ** backoff))

    def on_ws_frame(self, text: str, at: int) -> None:
        """The unfiltered ticker channel carries ~100+ frames/s across every Kalshi market (2,000 frames in
        well under 20 s on 2026-10-20, 4% of them for our universe), so frames outside the universe are
        counted and dropped before the raw log."""
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            self.counts["ws_unparseable"] += 1
            return
        typ = obj.get("type")
        self.counts[f"ws_{typ}"] += 1
        if typ == "ticker":
            ticker, top, ts = parse_ws_ticker(obj.get("msg") or {})
            if self._universe and ticker not in self._universe_set:
                self.counts["ws_ticker_outside_universe"] += 1
                return
            if self.rawlog is not None:
                self.rawlog.write(at_ms=at, direction="in", conn_id="ws", seq=None, text=text)
            self.store.begin()
            if self.store.insert_top(ticker, at, ts, top, "ws"):
                self.counts["top_rows"] += 1
            self.store.mark_seen([ticker], at, "ws")
            self._ws_tickers_seen += 1
            if self._ws_tickers_seen % 200 == 0:
                self.store.bump_minute(at, "ws", requests=0, tickers=200)
                self.store.commit()
        else:
            if self.rawlog is not None:
                self.rawlog.write(at_ms=at, direction="in", conn_id="ws", seq=None, text=text)
            if typ == "error":
                log.warning("kalshi ws error: %s", obj.get("msg"))



# --------------------------------------------------------------------- read API
_READERS: dict[str, KalshiBookStore] = {}     # path -> read-only store, opened on first use, closed at exit


def _reader(db: str | Path | KalshiBookStore | None) -> KalshiBookStore | None:
    """A cached read-only store for ``db`` (a path, or the default); None while the file does not exist
    (nothing is created: the feed owns the file, readers only ever open it read-only)."""
    if isinstance(db, KalshiBookStore):
        return db
    path = Path(db or _default_db_path())
    key = str(path)
    s = _READERS.get(key)
    if s is None:
        if not path.exists():
            return None
        s = _READERS[key] = KalshiBookStore(path, readonly=True)
    return s


def close_readers() -> None:
    for s in _READERS.values():
        try:
            s.close()
        except sqlite3.Error:
            pass
    _READERS.clear()


atexit.register(close_readers)


def _default_db_path() -> Path:
    import os
    return Path(os.environ.get("KALSHI_BOOKS_DB_PATH") or DEFAULT_DB_PATH)


def kalshi_quotes(tickers: Iterable[str], at_ms: int | None = None, *, db: str | Path | KalshiBookStore | None = None) -> dict[str, KalshiQuote]:
    """Latest quote per ticker, or the latest at-or-before ``at_ms`` (never a future row). {} when there is no db."""
    store = _reader(db)
    return {} if store is None else store.quotes(tickers, at_ms)


def kalshi_for_legs(position_ids: Iterable[str], at_ms: int | None = None, *,
                    db: str | Path | KalshiBookStore | None = None) -> dict[str, tuple[MatchRow, KalshiQuote | None]]:
    """position_id -> (MatchRow, KalshiQuote | None) through leg_matches. Legs with no leg_matches row are
    absent; legs matched with confidence ``none`` are present with quote None. {} when there is no db."""
    store = _reader(db)
    if store is None:
        return {}
    matches = store.matches(position_ids)
    quotes = store.quotes([m.kalshi_ticker for m in matches.values() if m.kalshi_ticker], at_ms)
    return {pid: (m, quotes.get(m.kalshi_ticker) if m.kalshi_ticker else None) for pid, m in matches.items()}


# ------------------------------------------------------------------------ stats
def stats_lines(store: KalshiBookStore, rfq_db: str | Path | None = None, *, now: int | None = None) -> list[str]:
    now = now or now_ms()
    c = store.conn
    lines = []
    universe = open_kalshi_tickers(rfq_db) if rfq_db and Path(rfq_db).exists() else []
    swept = {r[0] for r in c.execute("SELECT DISTINCT ticker FROM kalshi_top")}
    changed5 = {r[0] for r in c.execute("SELECT DISTINCT ticker FROM kalshi_top WHERE at_ms >= ?", (now - 300_000,))}
    recent5 = store.seen_since(now - 300_000)          # observed (rows are change-only, so this is the freshness key)
    last_row = store.scalar("SELECT MAX(at_ms) FROM kalshi_top")
    last_seen = store.scalar("SELECT MAX(last_seen_ms) FROM ticker_seen")
    lines.append(f"kalshi_top rows: {store.scalar('SELECT COUNT(*) FROM kalshi_top')}  tickers ever: {len(swept)}  "
                 f"open universe: {len(universe)}  last row: {('%.0fs ago' % ((now - last_row) / 1000)) if last_row else 'never'}"
                 f"  last observation: {('%.0fs ago' % ((now - last_seen) / 1000)) if last_seen else 'never'}")
    if universe:
        u = set(universe)
        lines.append(f"  universe observed in last 5 min: {len(recent5 & u)}/{len(u)} ({len(recent5 & u) / len(u):.1%})"
                     f"  (top changed: {len(changed5 & u)})")
    src = Counter({r[0]: r[1] for r in c.execute("SELECT source, COUNT(*) FROM kalshi_top WHERE at_ms >= ? GROUP BY source", (now - 3_600_000,))})
    lines.append("  rows last hour by source: " + (", ".join(f"{k}={v}" for k, v in sorted(src.items())) or "none"))
    mc = store.match_counts()
    matched = {r[0] for r in c.execute("SELECT DISTINCT kalshi_ticker FROM leg_matches WHERE kalshi_ticker IS NOT NULL")}
    lines.append(f"leg_matches: {sum(mc.values())} legs  " + "  ".join(f"{k}={mc.get(k, 0)}" for k in CONFIDENCES)
                 + f"  distinct tickers {len(matched)}  observed in last 5 min {len(matched & recent5)}"
                 + f"  refreshed {('%.0fs ago' % ((now - store.get_meta('matches_refreshed_ms', 0)) / 1000)) if store.get_meta('matches_refreshed_ms') else 'never'}")
    by_sport = Counter({(r[0], r[1]): r[2] for r in c.execute("SELECT sport, confidence, COUNT(*) FROM leg_matches GROUP BY sport, confidence")})
    for sport in sorted({s for s, _ in by_sport}):
        lines.append(f"  {sport:<5} " + "  ".join(f"{k}={by_sport.get((sport, k), 0)}" for k in CONFIDENCES))
    for window, label in ((300_000, "5 min"), (3_600_000, "1 h")):
        rows = c.execute("SELECT kind, SUM(requests), SUM(n429), SUM(errors), SUM(tickers) FROM request_minutes WHERE minute_ms >= ? GROUP BY kind",
                         (now - window,)).fetchall()
        tot = sum(r[1] for r in rows)
        lines.append(f"requests last {label}: {tot} ({tot / (window / 1000):.2f}/s)  429s {sum(r[2] for r in rows)}  errors {sum(r[3] for r in rows)}  "
                     + ", ".join(f"{r[0]} {r[1]} req/{r[4]} tickers" for r in rows))
    return lines


# -------------------------------------------------------------------- entrypoint
def build_feed(settings, *, db_path: str | Path | None = None, rfq_db: str | Path | None = None, use_ws: bool | None = None,
               auth: KalshiAuth | None = None) -> tuple[KalshiBookStore, KalshiFeed]:
    store = KalshiBookStore(db_path or settings.kalshi_books_db_path)
    auth = auth or KalshiAuth.from_settings(settings)
    client = KalshiFeedClient(settings.kalshi_api_url, auth=auth)
    rawlog = RawLog(settings.kalshi_log_dir) if settings.kalshi_log_dir else None
    sweep = settings.kalshi_sweep_sec if auth else max(settings.kalshi_sweep_sec, settings.kalshi_sweep_sec_unauth)
    ws = settings.kalshi_ws if use_ws is None else use_ws
    feed = KalshiFeed(store, client, rfq_db or settings.db_path, sweep_sec=sweep, depth_sec=settings.kalshi_depth_sec,
                      match_refresh_sec=settings.kalshi_match_refresh_sec, universe_hours=settings.book_universe_hours,
                      rawlog=rawlog, ws_url=settings.kalshi_ws_url if ws else None)
    return store, feed


def main(argv: list[str] | None = None) -> int:
    import argparse
    import signal
    import sys
    from .config import load_settings

    ap = argparse.ArgumentParser(prog="rfqbot.kalshi_books", description="Kalshi price feed, leg matches, stats")
    ap.add_argument("--env", default=".env"); ap.add_argument("--db", default=None); ap.add_argument("--rfq-db", default=None)
    ap.add_argument("--stats", action="store_true"); ap.add_argument("--refresh-matches", action="store_true")
    ap.add_argument("--no-ws", action="store_true"); ap.add_argument("--no-auth", action="store_true")
    ap.add_argument("--duration", type=float, default=None); ap.add_argument("--log-level", default=None)
    args = ap.parse_args(argv)
    settings = load_settings(args.env)
    logging.basicConfig(level=getattr(logging, (args.log_level or settings.log_level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING); logging.getLogger("websockets").setLevel(logging.WARNING)
    db_path = args.db or settings.kalshi_books_db_path
    rfq_db = args.rfq_db or settings.db_path

    if args.stats:
        if not Path(db_path).exists():
            print(f"no database at {db_path}", file=sys.stderr); return 2
        store = KalshiBookStore(db_path, readonly=True)
        try:
            print("\n".join(stats_lines(store, rfq_db)))
        finally:
            store.close()
        return 0
    if args.refresh_matches:
        store = KalshiBookStore(db_path)
        try:
            res = refresh_leg_matches(store, rfq_db)
            print(json.dumps(res, indent=1))
        finally:
            store.close()
        return 0

    auth = None if args.no_auth else KalshiAuth.from_settings(settings)
    store, feed = build_feed(settings, db_path=db_path, rfq_db=rfq_db, use_ws=False if args.no_ws else None, auth=auth)
    log.info("kalshi feed db=%s raw_log=%s auth=%s sweep=%.0fs depth=%.0fs ws=%s", store.path, settings.kalshi_log_dir or "off",
             "yes" if feed.client.auth else "no", feed.sweep_sec, feed.depth_sec, "on" if feed.ws_url and feed.client.auth else "off")

    async def run() -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, feed.stop)
        if args.duration:
            loop.call_later(args.duration, feed.stop)

        async def summary():
            prev: Counter = Counter()
            while True:
                await asyncio.sleep(60)
                cur = feed.counts + feed.client.counts
                delta = {k: cur[k] - prev.get(k, 0) for k in sorted(cur) if cur[k] - prev.get(k, 0)}
                prev = Counter(cur)
                log.info("last 60s: %s | universe=%d rate=%.2f req/s 429s=%d", delta, len(feed.universe),
                         feed.client.rate_per_sec(), feed.client.counts["n429"])

        t = asyncio.create_task(summary())
        try:
            await feed.run()
            return 0
        finally:
            t.cancel(); store.close()

    return asyncio.run(run())


if __name__ == "__main__":
    import sys
    sys.exit(main())
