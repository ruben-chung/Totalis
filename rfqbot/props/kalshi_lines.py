"""Settled Kalshi NBA player-prop markets and their last pre-tip price (public API, no auth).

    GET /markets?series_ticker=KXNBAPTS&status=settled&limit=1000[&cursor=]      floor_strike, result, expiration_value
    GET /series/{series}/markets/{ticker}/candlesticks?start_ts&end_ts&period_interval=1   (<= 5000 candles per call)

Verified 2026-10-20: both endpoints answer unauthenticated; the public rate limit is about one
request per second (429 otherwise). Settled props only go back to 2026-10-06 (the series are new
this season), so this source covers the current preseason and season, not 2025-26.

Ticker grammar is parsed by rfqbot.sports.nba_kalshi.parse_kalshi_nba (team, player initial +
surname, line); the line is `floor_strike` (19.5 -> "20+"), result yes | no, or `scalar` with an
empty expiration_value when the player did not play (settled to a fair price) -> stored as void.
`expiration_value` is the player's actual stat when settled yes / no.

Pre-tip price: the last 1-minute candle ending at or before tip (tip from the ESPN game when the
game is in `games`, else the market close time): mid of yes_bid / yes_ask closes when both sides
exist, else the last trade price. Candle windows are capped at 5000 minutes before tip.
"""
from __future__ import annotations

import gzip
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import httpx

from ..sports.nba_kalshi import KalshiParseError, parse_kalshi_nba
from .store import PropsStore

log = logging.getLogger(__name__)

API = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_RAW_DIR = Path("./data/props/raw/kalshi")
SERIES = ("KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT")
FAMILY_STAT = {"player_points": "pts", "player_rebounds": "reb", "player_assists": "ast", "player_threes": "fg3m"}
MAX_CANDLES = 5000


class KalshiPublicError(RuntimeError):
    pass


def _ts(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _f(x: Any) -> float | None:
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


class KalshiPublic:
    def __init__(self, *, base_url: str = API, raw_dir: str | Path = DEFAULT_RAW_DIR, min_gap_s: float = 1.1,
                 timeout: float = 30.0, client: httpx.Client | None = None):
        self.base_url = base_url
        self.raw_dir = Path(raw_dir)
        self.min_gap_s = min_gap_s
        self._last = 0.0
        self.client = client or httpx.Client(timeout=timeout, headers={"Accept": "application/json"})
        self.requests = 0

    def close(self) -> None:
        self.client.close()

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        for attempt in range(6):
            gap = self.min_gap_s - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()
            self.requests += 1
            r = self.client.get(self.base_url + path, params=params)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(3.0 * (attempt + 1))
                continue
            raise KalshiPublicError(f"{path} -> {r.status_code}: {r.text[:200]}")
        raise KalshiPublicError(f"{path}: gave up after retries")

    def settled_markets(self, series: str) -> list[dict[str, Any]]:
        out, cursor = [], None
        while True:
            params = {"series_ticker": series, "status": "settled", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            doc = self.get("/markets", params)
            out.extend(doc.get("markets", []))
            cursor = doc.get("cursor")
            if not cursor:
                break
        path = self.raw_dir / "settled" / f"{series}_{int(time.time())}.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(out, f)
        return out

    def candles(self, series: str, ticker: str, start_ts: int, end_ts: int, *, period: int = 1) -> list[dict[str, Any]]:
        path = self.raw_dir / "candles" / f"{ticker}.json.gz"
        if path.exists():
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        doc = self.get(f"/series/{series}/markets/{ticker}/candlesticks",
                       {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period})
        cs = doc.get("candlesticks", [])
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(cs, f)
        return cs


# ------------------------------------------------------------------ parsing
def market_to_line(m: dict[str, Any]) -> dict[str, Any] | None:
    """One settled market -> prop_lines row (without price); None when the ticker is not a player prop."""
    try:
        spec = parse_kalshi_nba(m["ticker"], m.get("title") or "", m.get("floor_strike"), m.get("strike_type"),
                                m.get("rules_primary") or "", m.get("expected_expiration_time"),
                                yes_sub_title=m.get("yes_sub_title"), rules_secondary=m.get("rules_secondary"))
    except (KalshiParseError, ValueError) as e:
        log.debug("skip %s: %s", m.get("ticker"), e)
        return None
    stat = FAMILY_STAT.get(spec.family)
    if stat is None or spec.line is None:
        return None
    result = m.get("result")
    if result not in ("yes", "no"):
        result = "void" if result else None
    return {
        "source": "kalshi", "ticker": m["ticker"], "player_key": spec.bucket, "player_name": spec.venue_ids.get("player"),
        "team": spec.venue_ids.get("kalshi_team") or spec.team, "game_key": f"nba|{spec.game_date}|{spec.away}@{spec.home}",
        "stat": stat, "line": float(spec.line), "p_over": None, "t": None, "result": result,
        "actual": _f(m.get("expiration_value")) if result in ("yes", "no") else None,
        "open_time": m.get("open_time"), "close_time": m.get("close_time"),
    }


def pre_tip_price(candles: Iterable[dict[str, Any]], tip_ts: int) -> tuple[float | None, int | None]:
    """Last candle ending at or before tip -> (price, ts). Mid of the closes when both sides are quoted."""
    best = None
    for c in candles:
        end = int(c.get("end_period_ts") or 0)
        if end <= tip_ts and (best is None or end > int(best.get("end_period_ts") or 0)):
            best = c
    if best is None:
        return None, None
    bid = _f((best.get("yes_bid") or {}).get("close_dollars"))
    ask = _f((best.get("yes_ask") or {}).get("close_dollars"))
    price = best.get("price") or {}
    last = _f(price.get("close_dollars")) or _f(price.get("previous_dollars"))
    if bid is not None and ask is not None and bid > 0 and ask < 1:
        return (bid + ask) / 2, int(best["end_period_ts"])
    if last is not None and last > 0:
        return last, int(best["end_period_ts"])
    if bid is not None and bid > 0:
        return bid, int(best["end_period_ts"])
    if ask is not None and ask < 1:
        return ask, int(best["end_period_ts"])
    return None, int(best["end_period_ts"])


def pull_kalshi_lines(store: PropsStore, client: KalshiPublic, *, series: Iterable[str] = SERIES, candles: bool = True,
                      refetch: bool = False, out=print) -> dict[str, int]:
    n = {"markets": 0, "lines": 0, "unparsed": 0, "priced": 0, "no_candles": 0, "tip_from_close": 0}
    tips = store.game_tips()
    for s in series:
        markets = client.settled_markets(s)
        n["markets"] += len(markets)
        rows = []
        for m in markets:
            row = market_to_line(m)
            if row is None:
                n["unparsed"] += 1
                continue
            rows.append(row)
        out(f"{s}: {len(markets)} settled markets, {len(rows)} parsed")
        if candles:
            for row in rows:
                if not refetch and store.fetched("kalshi_candles", row["ticker"]) == "ok":
                    prev = store.rows("SELECT p_over, t FROM prop_lines WHERE source='kalshi' AND ticker=?", row["ticker"])
                    if prev:
                        row["p_over"], row["t"] = prev[0]["p_over"], prev[0]["t"]
                        if row["p_over"] is not None:
                            n["priced"] += 1
                        continue
                tip_iso = tips.get(row["game_key"])
                if tip_iso is None:
                    n["tip_from_close"] += 1
                tip_ts = _ts(tip_iso) or _ts(row["close_time"]) or 0
                start = max(_ts(row["open_time"]) or 0, tip_ts - MAX_CANDLES * 60 + 60)
                try:
                    cs = client.candles(s, row["ticker"], start, tip_ts)
                except KalshiPublicError as e:
                    store.log_fetch("kalshi_candles", row["ticker"], "error", str(e)[:200])
                    out(f"candles {row['ticker']}: {e}")
                    continue
                row["p_over"], row["t"] = pre_tip_price(cs, tip_ts)
                if row["p_over"] is None:
                    n["no_candles"] += 1
                else:
                    n["priced"] += 1
                store.log_fetch("kalshi_candles", row["ticker"], "ok", f"{len(cs)} candles")
        n["lines"] += store.upsert("prop_lines", rows)
        store.log_fetch("kalshi_series", s, "ok", f"{len(markets)} markets")
    return n
