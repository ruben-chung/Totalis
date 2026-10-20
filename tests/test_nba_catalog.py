"""Parse every NBA row in data/rfq.db (skipped when the file is absent) and every synced Kalshi NBA row."""
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from rfqbot.sports.legspec import canonical_key
from rfqbot.sports.nba_kalshi import parse_kalshi_nba
from rfqbot.sports.nba_poly import PolyParseError, parse_poly_nba

DB = Path(__file__).resolve().parent.parent / "data" / "rfq.db"
pytestmark = pytest.mark.skipif(not DB.exists(), reason="data/rfq.db not present")


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_every_nba_catalog_row_parses(conn):
    rows = conn.execute("SELECT * FROM markets WHERE slug LIKE 'nba-%' ORDER BY slug, outcome_index").fetchall()
    if not rows:
        pytest.skip("no NBA rows in catalog")
    failures = []
    keys: Counter = Counter()
    fams: Counter = Counter()
    for r in rows:
        try:
            s = parse_poly_nba(r["slug"], r["title"], r["outcome"], r["outcome_index"], position_id=r["position_id"])
        except PolyParseError as e:
            failures.append(f"{r['slug']} | {r['title']} | {r['outcome']} | {e}")
            continue
        keys[canonical_key(s)] += 1
        fams[(s.family, s.period)] += 1
    assert not failures, "\n".join(failures[:40])
    assert not [k for k, n in keys.items() if n > 1], "duplicate canonical keys"
    assert {"moneyline", "spread", "total", "odd_even", "team_to_score_first", "player_points"} <= {f for f, _ in fams}
    tagged = conn.execute("SELECT COUNT(*) FROM markets WHERE tags LIKE '%\"nba\"%' AND slug NOT LIKE 'nba-%'").fetchone()[0]
    assert tagged == 0, "rows tagged nba with a non-nba slug"


def test_every_synced_kalshi_nba_row_reparses(conn):
    try:
        rows = conn.execute("SELECT * FROM kalshi_markets WHERE sport='nba'").fetchall()
    except sqlite3.OperationalError:
        pytest.skip("no kalshi_markets table")
    if not rows:
        pytest.skip("no Kalshi NBA rows synced")
    # kalshi-sync upserts open markets only, so rows whose market closed before the latest sync may carry
    # keys from an older parser version; the stored-key check is limited to the latest sync batch.
    latest = max(r["synced_at_ms"] for r in rows)
    bad = []
    for r in rows:
        try:
            s = parse_kalshi_nba(r["ticker"], r["title"] or "", r["floor_strike"], r["strike_type"], r["rules_primary"] or "",
                                 r["expected_expiration_time"], yes_sub_title=r["yes_sub_title"], rules_secondary=r["rules_secondary"])
        except ValueError as e:
            bad.append(f"{r['ticker']}: {e}")
            continue
        if r["synced_at_ms"] == latest:
            assert r["parse_ok"] == 1 and r["canonical_key"] == canonical_key(s), r["ticker"]
    assert not bad, "\n".join(bad[:20])
