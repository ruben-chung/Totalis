"""python -m rfqbot.marginals --game nfl-ne-chi-2026-10-23 [--at-ms T] [--kalshi auto|none|synthetic] [--log]

Prints one line per leg (p, err, weights, tier, flags, explain). --log appends every computed
marginal with its per-venue features to data/marginals.db (table `marginals`) for calibration.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from . import LegMarginal, compute_game, load_config
from .model import KALSHI, POLY, Marginal
from .sources import sport_of

DEFAULT_LOG_DB = Path("./data/marginals.db")

# (column, sql type) in insert order; new columns are added to an existing table by log_rows
LOG_COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_ms", "INTEGER NOT NULL"), ("eval_ms", "INTEGER NOT NULL"), ("game", "TEXT NOT NULL"), ("sport", "TEXT"),
    ("position_id", "TEXT NOT NULL"), ("slug", "TEXT"), ("title", "TEXT"), ("outcome", "TEXT"),
    ("p", "REAL"), ("err", "REAL"), ("p_poly", "REAL"), ("p_kalshi", "REAL"), ("w_poly", "REAL"), ("w_kalshi", "REAL"),
    ("kalshi_confidence", "TEXT"), ("kalshi_ticker", "TEXT"), ("kalshi_side", "TEXT"),
    ("tier", "TEXT NOT NULL"), ("flags", "TEXT NOT NULL"), ("inplay", "INTEGER NOT NULL"), ("at_ms", "INTEGER NOT NULL"),
    ("poly_bid", "REAL"), ("poly_ask", "REAL"), ("poly_bid_size", "REAL"), ("poly_ask_size", "REAL"),
    ("poly_bid_depth", "REAL"), ("poly_ask_depth", "REAL"), ("poly_at_ms", "INTEGER"),
    ("poly_depth", "REAL"), ("poly_staleness_s", "REAL"), ("poly_sigma", "REAL"), ("poly_precision", "REAL"),
    ("kalshi_yes_bid", "REAL"), ("kalshi_yes_ask", "REAL"), ("kalshi_bid_size", "REAL"), ("kalshi_ask_size", "REAL"),
    ("kalshi_bid_depth", "REAL"), ("kalshi_ask_depth", "REAL"), ("kalshi_at_ms", "INTEGER"),
    ("kalshi_depth", "REAL"), ("kalshi_staleness_s", "REAL"), ("kalshi_sigma", "REAL"), ("kalshi_precision", "REAL"),
    ("explain", "TEXT"), ("config", "TEXT NOT NULL"),
    ("poly_age_s", "REAL"), ("kalshi_age_s", "REAL"), ("poly_confirmed_ms", "INTEGER"), ("kalshi_confirmed_ms", "INTEGER"),
)


def _ensure_log_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS marginals (" + ", ".join(f"{c} {t}" for c, t in LOG_COLUMNS) + ")")
    have = {r[1] for r in conn.execute("PRAGMA table_info(marginals)")}
    for c, t in LOG_COLUMNS:
        if c not in have:
            conn.execute(f"ALTER TABLE marginals ADD COLUMN {c} {t.replace(' NOT NULL', '')}")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_marginals_game_time ON marginals(game, eval_ms)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_marginals_pid_time ON marginals(position_id, eval_ms)")


def _venue(m: Marginal, name: str):
    for v in m.venues:
        if v.venue == name:
            return v
    return None


def log_record(lm: LegMarginal, *, run_ms: int, eval_ms: int, game: str, sport: str | None, config_json: str,
               poly_confirmed_ms: int | None = None, kalshi_confirmed_ms: int | None = None) -> dict:
    q, row, kq, m = lm.quote, lm.match, lm.kalshi, lm.marginal
    ep, ek = _venue(m, POLY), _venue(m, KALSHI)
    return {
        "run_ms": run_ms, "eval_ms": eval_ms, "game": game, "sport": sport,
        "position_id": q.position_id, "slug": q.slug, "title": q.title, "outcome": q.outcome,
        "p": m.p, "err": m.err, "p_poly": m.p_poly, "p_kalshi": m.p_kalshi, "w_poly": m.w_poly, "w_kalshi": m.w_kalshi,
        "kalshi_confidence": m.kalshi_confidence, "kalshi_ticker": getattr(row, "kalshi_ticker", None),
        "kalshi_side": getattr(row, "kalshi_side", None),
        "tier": m.tier, "flags": ",".join(m.flags), "inplay": int("inplay" in m.flags), "at_ms": m.at_ms,
        "poly_bid": q.best_bid, "poly_ask": q.best_ask, "poly_bid_size": q.bid_size, "poly_ask_size": q.ask_size,
        "poly_bid_depth": q.bid_depth, "poly_ask_depth": q.ask_depth, "poly_at_ms": q.at_ms,
        "poly_depth": ep.depth if ep else None, "poly_staleness_s": ep.staleness_s if ep else None,
        "poly_sigma": ep.sigma if ep else None, "poly_precision": ep.precision if ep else None,
        "kalshi_yes_bid": getattr(kq, "yes_bid", None), "kalshi_yes_ask": getattr(kq, "yes_ask", None),
        "kalshi_bid_size": getattr(kq, "bid_size", None), "kalshi_ask_size": getattr(kq, "ask_size", None),
        "kalshi_bid_depth": getattr(kq, "bid_depth", None), "kalshi_ask_depth": getattr(kq, "ask_depth", None),
        "kalshi_at_ms": getattr(kq, "at_ms", None),
        "kalshi_depth": ek.depth if ek else None, "kalshi_staleness_s": ek.staleness_s if ek else None,
        "kalshi_sigma": ek.sigma if ek else None, "kalshi_precision": ek.precision if ek else None,
        "explain": m.explain, "config": config_json,
        "poly_age_s": ep.age_s if ep else None, "kalshi_age_s": ek.age_s if ek else None,
        "poly_confirmed_ms": poly_confirmed_ms, "kalshi_confirmed_ms": kalshi_confirmed_ms,
    }


def log_rows(db: str | Path, records: list[dict]) -> int:
    conn = sqlite3.connect(str(db))
    try:
        _ensure_log_schema(conn)
        cols = [c for c, _ in LOG_COLUMNS]
        conn.executemany(f"INSERT INTO marginals ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         [tuple(r[c] for c in cols) for r in records])
        conn.commit()
        return len(records)
    finally:
        conn.close()


def format_table(rows: list[LegMarginal], *, game: str = "", width: int = 44) -> str:
    """One line per leg plus its explain line; slugs are shown without the game prefix."""
    lines = [f"{'leg':<{width}} {'p':>6} {'err':>6} {'wP':>5} {'wK':>5} {'tier':<8} flags / explain"]
    for lm in rows:
        q, m = lm.quote, lm.marginal
        if game and q.slug == game:
            slug = "moneyline"
        elif game and q.slug.startswith(game + "-"):
            slug = q.slug[len(game) + 1:]
        else:
            slug = q.slug
        leg = f"{slug} / {q.outcome}"
        if len(leg) > width:
            leg = leg[: width - 1] + "…"
        p = "-" if m.p is None else f"{m.p:.3f}"
        e = "-" if m.err is None else f"{m.err:.3f}"
        lines.append(f"{leg:<{width}} {p:>6} {e:>6} {m.w_poly:>5.2f} {m.w_kalshi:>5.2f} {m.tier:<8} {','.join(m.flags)}")
        lines.append(f"{'':<{width}} {m.explain}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rfqbot.marginals", description=__doc__.splitlines()[0])
    ap.add_argument("--game", required=True, help="game slug prefix, e.g. nfl-ne-chi-2026-10-23")
    ap.add_argument("--at-ms", type=int, default=None, help="evaluate point-in-time at this epoch ms (no future quotes)")
    ap.add_argument("--kalshi", default="auto", choices=("auto", "none", "synthetic"))
    ap.add_argument("--books-db", default="./data/books.db")
    ap.add_argument("--rfq-db", default="./data/rfq.db")
    ap.add_argument("--kalshi-db", default=None, help="kalshi_books.db ($KALSHI_BOOKS_DB_PATH or ./data/kalshi_books.db)")
    ap.add_argument("--log", action="store_true", help="append rows to --log-db")
    ap.add_argument("--log-db", default=str(DEFAULT_LOG_DB))
    ap.add_argument("--tier", default=None, help="only print legs with this tier")
    ap.add_argument("--flag", default=None, help="only print legs carrying this flag")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    cfg = load_config()
    now_ms = int(time.time() * 1000)
    eval_ms = args.at_ms if args.at_ms is not None else now_ms
    rows = compute_game(args.game, args.at_ms, cfg=cfg, books_db=args.books_db, rfq_db=args.rfq_db,
                        kalshi_mode=args.kalshi, kalshi_db=args.kalshi_db, now_ms=now_ms)
    if not rows:
        print(f"no markets with tokens under {args.game!r} (catalog/leg_tokens empty for it)", file=sys.stderr)
        return 1
    sport = sport_of(args.game)

    shown = [r for r in rows if (args.tier is None or r.marginal.tier == args.tier)
             and (args.flag is None or args.flag in r.marginal.flags)]
    if args.limit:
        shown = shown[: args.limit]
    print(format_table(shown, game=args.game))
    tiers: dict[str, int] = {}
    for r in rows:
        tiers[r.marginal.tier] = tiers.get(r.marginal.tier, 0) + 1
    n_k = sum(1 for r in rows if r.marginal.w_kalshi > 0)
    print(f"\n{len(rows)} legs  eval_ms={eval_ms}  sport={sport}  kalshi={args.kalshi} (used on {n_k})  "
          + "  ".join(f"{t}={n}" for t, n in sorted(tiers.items())))
    if args.log:
        from .sources import kalshi_confirmation, poly_confirmations
        confirmed = poly_confirmations([r.quote for r in rows], args.at_ms, books_db=args.books_db)
        k_conf = kalshi_confirmation(args.at_ms, db=args.kalshi_db) if args.kalshi == "auto" else None
        recs = [log_record(r, run_ms=now_ms, eval_ms=eval_ms, game=args.game, sport=sport, config_json=cfg.to_json(),
                           poly_confirmed_ms=confirmed.get(r.quote.position_id), kalshi_confirmed_ms=k_conf) for r in rows]
        n = log_rows(args.log_db, recs)
        print(f"logged {n} rows to {args.log_db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
