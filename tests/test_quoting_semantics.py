import json
import sqlite3
from pathlib import Path

import pytest

from rfqbot.quoting.semantics import (BUY_SAFETY_MAX, MAKER_BUYS_YES, MAKER_SELLS_YES, UnsupportedRfq, maker_view,
                                      notional_e6, requested_shares_e6)

DB = Path(__file__).resolve().parent.parent / "data" / "rfq.db"


def test_maker_side_mapping():
    assert maker_view("BUY", "YES", "notional").maker_action == MAKER_SELLS_YES
    assert maker_view("SELL", "YES", "shares").maker_action == MAKER_BUYS_YES
    assert maker_view("BUY", "YES", "").size_unit == "notional"
    for d, s, u in (("BUY", "NO", "notional"), ("SELL", "YES", "notional"), ("BUY", "YES", "shares"), ("HOLD", "YES", "")):
        with pytest.raises(UnsupportedRfq):
            maker_view(d, s, u)


def test_size_units():
    # docs example: notional 2.0 pUSD at 0.391 -> 5.115 shares
    assert requested_shares_e6("notional", 2_000_000, 391_000) == 5_115_089
    assert requested_shares_e6("shares", 7_000_000, 391_000) == 7_000_000
    assert notional_e6(5_115_089, 391_000) == 1_999_999
    assert BUY_SAFETY_MAX == 0.90909


@pytest.mark.skipif(not DB.exists(), reason="data/rfq.db not present")
def test_recorded_frames_agree_with_semantics():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    assert c.execute("SELECT COUNT(*) FROM rfqs WHERE side IS NOT NULL AND side != 'YES'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM rfqs WHERE direction='BUY' AND size_unit NOT IN ('notional')").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM rfqs WHERE direction='SELL' AND size_unit NOT IN ('shares')").fetchone()[0] == 0
    rows = c.execute("""SELECT t.direction, r.size_value_e6, t.price_e6, t.size_e6 FROM trades t JOIN rfqs r ON r.rfq_id=t.rfq_id
                        WHERE r.size_value_e6 > 0 AND t.price_e6 > 0""").fetchall()
    if rows:
        for d, v, p, s in rows:
            if d == "BUY":          # shares * price <= notional budget (fees included in the budget)
                assert s * p / 1e6 <= v * 1.001, (v, p, s)
            else:                   # shares filled <= shares requested
                assert s <= v * 1.001, (v, s)
    for (t,) in c.execute("SELECT payload FROM raw_messages WHERE msg_type='RFQ_TRADE' LIMIT 20"):
        o = json.loads(t)
        assert o["side"] == "YES" and int(o["price_e6"]) <= 1_000_000 and int(o["size_e6"]) > 0
