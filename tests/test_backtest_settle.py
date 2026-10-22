"""Settlement (void and unresolved legs, history fallback, re-runnable), attribution, game-clustered statistics, compare."""
import json
import sqlite3
from argparse import Namespace

import pytest

from rfqbot.backtest.compare import SUITE, fmt_compare, run_compare
from rfqbot.backtest.config import BacktestConfig, WindowConfig
from rfqbot.backtest.report import attribution, build_report, fill_table, fmt, funnel, margin_terms, pnl_section
from rfqbot.backtest.settle import combo_status, leg_status, load_resolutions, settle_run, Resolution
from rfqbot.backtest.stats import cluster_ids, cluster_mean_se, cluster_total_se, n_games, verdict
from rfqbot.backtest.store import ALL_DECISION_COLS, BacktestStore
from rfqbot.store import Store

from test_backtest_replay import LEGS, world
from backtest_world import T0, add_print

E6 = 1_000_000


def leg(pid, cond, outcome, slug="nba-gsw-lal-2026-10-21"):
    return {"position_id": pid, "slug": slug, "outcome": outcome, "game_key": slug[:22], "p": 0.5, "err": 0.01, "tier": "ok", "flags": [],
            "kalshi_confidence": "none", "key": None, "condition_id": cond}


def drow(rfq_id, legs, *, run_id="r", action="sell_yes", price=0.52, fair=0.50, size=100.0, at=T0, cond="c1", games=("g1",),
         source=None, u_m=0.01, u_d=0.0, e_b=0.005, e_i=0.0, margin=None, curse=None, info=None, wallet=None, direction="BUY"):
    margin = 0.015 if margin is None else margin
    r = {c: None for c in ALL_DECISION_COLS}
    r.update(run_id=run_id, rfq_id=rfq_id, at_ms=at, direction=direction, n_legs=len(legs), legs_json=json.dumps(legs), clusters_json="[]",
             p_fair=fair, u_m=u_m, u_d=u_d, e_b=e_b, e_i=e_i, margin=margin, maker_action=action, price_e6=int(round(price * E6)),
             size_e6=int(size * E6), decision="quoted", condition_id=cond, games_json=json.dumps(list(games)), sport="nba",
             pair_class="disjoint", printed=1, fill_win=1, fill_strict=1, fill_frac=1.0, fill_size_e6=int(size * E6),
             edge_source=source, curse_mult=curse, info_premium=info, wallet_premium=wallet, executed_at_ms=at + 1000, latency_ms=2.0)
    return r


def make_store(tmp_path, rows, run_id="r", quote=None):
    st = BacktestStore(tmp_path / "bt.db")
    st.start_run(run_id, "n", BacktestConfig().to_json(), json.dumps(quote or {"k_m": 1.0, "k_d": 1.0}), WindowConfig(0, 1).to_json())
    st.insert_decisions(rows)
    st.finish_run(run_id, {"prefilter": {"requests": 10, "pre:other_sport": 5}, "quoted": len(rows)})
    return st


def resolutions_db(tmp_path, rows):
    s = Store(tmp_path / "rfq.db")
    s.conn.executemany("INSERT OR REPLACE INTO market_resolutions(condition_id, resolved_outcome, resolved_at_ms) VALUES (?,?,?)", rows)
    s.close()
    return tmp_path / "rfq.db"


def history_db(tmp_path, rows):
    p = tmp_path / "history.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE hist_markets (condition_id TEXT, outcomes TEXT, outcome_prices TEXT, resolved INTEGER)")
    c.executemany("INSERT INTO hist_markets VALUES (?,?,?,?)", rows)
    c.commit(); c.close()
    return p


def test_leg_and_combo_status_rules():
    r = Resolution("Yes", False, "x")
    assert leg_status("Yes", r) == "won" and leg_status("No", r) == "lost" and leg_status("Yes", None) is None
    assert leg_status("Yes", Resolution(None, True, "x")) == "void"
    assert combo_status(["won", "won"]) == ("won", 1.0)
    assert combo_status(["won", "lost"]) == ("lost", 0.0)
    assert combo_status([None, "lost"]) == ("lost", 0.0)                      # one losing leg decides the combo
    assert combo_status(["won", None]) == ("unresolved", None)
    assert combo_status(["won", "void"]) == ("void", None) and combo_status(["lost", "void"]) == ("void", None)
    assert combo_status([]) == ("unresolved", None)


def test_settle_void_unresolved_history_and_rerun(tmp_path):
    rows = [
        drow("won", [leg("a", "cA", "Yes"), leg("b", "cB", "Yes")], cond="k1", price=0.52, size=100),            # sold YES at .52, pays 1: -48
        drow("lost", [leg("a", "cA", "Yes"), leg("c", "cC", "Yes")], cond="k2", price=0.52, size=100),           # leg c unresolved but a... see below
        drow("void", [leg("d", "cD", "Yes"), leg("b", "cB", "Yes")], cond="k3"),
        drow("open", [leg("e", "cE", "Yes"), leg("b", "cB", "Yes")], cond="k4", price=0.40, size=50, fair=0.38, action="buy_yes", direction="SELL"),
        drow("open", [leg("e", "cE", "Yes"), leg("b", "cB", "Yes")], cond="k4", at=T0 + 60_000, fair=0.30, run_id="r", price=0.30, size=0),  # later fair, no fill
        drow("hist", [leg("h", "cH", "Over"), leg("b", "cB", "Yes")], cond="k5"),
    ]
    rows[1]["legs_json"] = json.dumps([leg("a", "cA", "No"), leg("c", "cC", "Yes")])            # cA resolves Yes: this leg lost
    rows[4]["decision"] = "skipped:size_below_min"; rows[4]["fill_size_e6"] = 0
    st = make_store(tmp_path, rows)
    rfq = resolutions_db(tmp_path, [("cA", "Yes", 111), ("cB", "Yes", 222), ("cD", "50-50", 333)])
    hist = history_db(tmp_path, [("cH", '["Over", "Under"]', '["1", "0"]', 1), ("cX", '["Yes", "No"]', '["0.5", "0.5"]', 1)])
    s = settle_run(st, "r", rfq, hist)
    assert (s["fills"], s["won"], s["lost"], s["void"], s["unresolved"]) == (5, 2, 1, 1, 1)
    got = {r["rfq_id"]: r for r in st.conn.execute("SELECT * FROM settlements WHERE run_id='r'")}
    assert got["won"]["status"] == "won" and got["won"]["pnl"] == pytest.approx(-1 * 100 * (1 - 0.52))
    assert got["lost"]["status"] == "lost" and got["lost"]["pnl"] == pytest.approx(-1 * 100 * (0 - 0.52)) and got["lost"]["legs_resolved"] == 1
    assert got["void"]["status"] == "void" and got["void"]["pnl"] is None
    assert got["hist"]["status"] == "won" and got["hist"]["source"] == "hist_markets,market_resolutions"
    o = got["open"]                                                                              # bought YES at .40; marked to the later fair .30
    assert o["status"] == "unresolved" and o["mark"] == pytest.approx(0.30) and o["unrealized"] == pytest.approx(50 * (0.30 - 0.40))
    assert s["realized"] == pytest.approx(got["won"]["pnl"] + got["lost"]["pnl"] + got["hist"]["pnl"]) and s["unresolved_share"] == pytest.approx(1 / 5)
    assert s["unrealized"] == pytest.approx(o["unrealized"]) and s["mark_age_s_median"] == pytest.approx(0.0)
    # re-run after the Data Engineer's resolve refresh: cE resolves against us -> the open combo is decided, nothing is duplicated
    resolutions_db(tmp_path, [("cE", "No", 444)])
    s2 = settle_run(st, "r", rfq, hist)
    assert (s2["unresolved"], s2["lost"]) == (0, 2) and st.conn.execute("SELECT COUNT(*) FROM settlements WHERE run_id='r'").fetchone()[0] == 5
    op = st.conn.execute("SELECT * FROM settlements WHERE rfq_id='open'").fetchone()
    assert op["status"] == "lost" and op["pnl"] == pytest.approx(1 * 50 * (0 - 0.40)) and op["unrealized"] is None
    assert load_resolutions(rfq, hist, ["cX"])["cX"].void                                         # 0.5/0.5 in history is a void


def test_attribution_sums_to_the_total_and_terms_to_the_markup(tmp_path):
    fair, price = 0.5000, 0.5220
    rows = [
        # fitted node: margin0 = .004 + 1.2 * (.01 + .005) + .001 + .0005 = .0235 ; e_i = -.002 -> margin .0215 ; markup .022 -> rounding .0005
        drow("m", [leg("a", "cA", "Yes")], source="model", u_m=0.01, u_d=0.005, e_b=0.004, curse=1.2, info=0.001, wallet=0.0005, e_i=-0.002,
             margin=0.0215, price=price, fair=fair, size=100),
        # stage 2: margin0 = k_m u_m + k_d u_d + e_b = 1.5 * .01 + 2 * .005 + .005 = .03 ; e_i +.001 -> .031 ; markup .033 -> rounding .002
        drow("s", [leg("a", "cA", "No")], source=None, u_m=0.01, u_d=0.005, e_b=0.005, e_i=0.001, margin=0.031, price=0.533, fair=0.50, size=200,
             cond="k2"),
        # the skew floor: margin0 .02, e_i -.05 -> floored at 0, margin 0
        drow("f", [leg("a", "cA", "Yes")], source="measured", u_m=0.01, u_d=0.0, e_b=0.01, curse=1.0, info=0.0, wallet=0.0, e_i=-0.05, margin=0.0,
             price=0.5, fair=0.5, size=10, cond="k3", action="buy_yes", direction="SELL"),
    ]
    st = make_store(tmp_path, rows, quote={"k_m": 1.5, "k_d": 2.0})
    rfq = resolutions_db(tmp_path, [("cA", "Yes", 1)])
    settle_run(st, "r", rfq, None)
    qcfg = {"k_m": 1.5, "k_d": 2.0}
    t = margin_terms(st.conn.execute("SELECT * FROM decisions WHERE rfq_id='m'").fetchone(), qcfg)
    assert t == pytest.approx({"base": 0.004, "u_m": 0.012, "u_d": 0.006, "info": 0.001, "wallet": 0.0005, "inventory": -0.002, "floor": 0.0, "rounding": 0.0005})
    assert sum(t.values()) == pytest.approx(abs(price - fair))
    t2 = margin_terms(st.conn.execute("SELECT * FROM decisions WHERE rfq_id='s'").fetchone(), qcfg)
    assert t2["u_m"] == pytest.approx(0.015) and t2["u_d"] == pytest.approx(0.01) and t2["rounding"] == pytest.approx(0.002)
    t3 = margin_terms(st.conn.execute("SELECT * FROM decisions WHERE rfq_id='f'").fetchone(), qcfg)
    assert t3["floor"] == pytest.approx(0.03) and sum(t3.values()) == pytest.approx(0.0)
    ft = fill_table(st, "r")
    for f in ft:
        assert sum(f["terms"].values()) == pytest.approx(f["markup"])                         # terms in $ sum to the markup
        assert f["model_pnl"] == pytest.approx(f["pricing_error"] + f["markup"])              # payoff - price = (payoff - fair) + (fair - price)
    by = {f["rfq_id"]: f for f in ft}
    assert by["m"]["status"] == "won" and by["m"]["realized"] == pytest.approx(-100 * (1 - price))
    assert by["s"]["status"] == "lost" and by["f"]["realized"] == pytest.approx(10 * (1 - 0.5))
    a = attribution(ft)["all"]
    assert a["pnl"] == pytest.approx(sum(f["realized"] for f in ft)) and a["term_check"] == pytest.approx(0.0, abs=1e-9)
    assert a["pnl"] == pytest.approx(a["pricing_error"] + a["margin"]) and a["margin"] == pytest.approx(sum(a["margin_terms"].values()))
    assert attribution(ft)["unresolved_marked_to_model"]["n"] == 0


def test_report_funnel_pnl_and_text(tmp_path):
    rows = [drow(f"w{i}", [leg("a", "cA", "Yes" if i % 2 == 0 else "No")], cond=f"k{i}", games=(f"g{i % 3}",), price=0.52 if i % 2 == 0 else 0.48, fair=0.5, size=100,
                 direction="BUY" if i % 2 == 0 else "SELL", action="sell_yes" if i % 2 == 0 else "buy_yes") for i in range(6)]
    rows.append({**drow("skip", [leg("a", "cA", "Yes")]), "decision": "skipped:size_below_min", "p_fair": None, "fill_size_e6": 0, "printed": 0,
                 "fill_win": 0, "fill_strict": 0, "price_e6": None})
    st = make_store(tmp_path, rows)
    rfq = resolutions_db(tmp_path, [("cA", "Yes", 1)])
    settle_run(st, "r", rfq, None)
    f = funnel(st, "r")
    assert (f["requests_seen"], f["prefiltered_other_sport"], f["evaluated"], f["priced"], f["quoted"], f["filled"], f["win"], f["strict"]) == (10, 5, 7, 6, 6, 6, 6, 6)
    assert f["by_direction"]["BUY"]["quoted"] == 3 and f["skips"] == {"skipped:size_below_min": 1}
    p = pnl_section(fill_table(st, "r"))
    assert p["fills"] == 6 and p["resolved_combos"] == 6 and p["unresolved_share"] == 0.0 and p["decisive_games"] == 3
    assert p["realized"]["clusters"] == 3 and p["gross_markup"]["total"] == pytest.approx(6 * 100 * 0.02)
    rep = build_report(st, "r", rfq_db=rfq, books_db=tmp_path / "b.db", kalshi_db=tmp_path / "k.db", markouts=False)
    txt = fmt(rep)
    assert "gross markup" in txt and "attribution" in txt and "realized" in txt and "underpowered" in txt           # 3 game clusters < 10
    assert json.loads(json.dumps(rep, default=str))["pnl"]["resolved_combos"] == 6


def test_cluster_statistics():
    cl = cluster_ids([["g1"], ["g1", "g2"], ["g3"], ["g2"], ["g4", "g5"], []])
    assert cl[0] == cl[1] == cl[3] and cl[2] != cl[0] and cl[4] != cl[0] and cl[5].startswith("row:")      # g1-g2 fuse; g3, g4-g5, empty apart
    assert n_games([["g1"], ["g1", "g2"], []]) == 2
    mean, se, g = cluster_mean_se([1, 1, 1, 1], ["a", "a", "b", "b"])
    assert (mean, se, g) == (1.0, 0.0, 2)
    mean, se, g = cluster_mean_se([1, 1, 3, 3], ["a", "a", "b", "b"])                                     # cluster sums of deviations: -2 and +2
    assert mean == 2.0 and g == 2 and se == pytest.approx((2 / 1 * (4 + 4) / 16) ** 0.5)
    assert cluster_mean_se([1.0, 2.0], ["a", "a"]) == (1.5, None, 1) and cluster_mean_se([], []) == (0.0, None, 0)
    tot, tse, g = cluster_total_se([1, 1, 3, 3], ["a", "a", "b", "b"])
    assert tot == 8.0 and tse == pytest.approx(4 * 1.0)
    assert verdict(5.0, 1.0, 12) == "exceeds 2 SE" and verdict(1.0, 1.0, 12) == "within 2 SE"
    assert verdict(5.0, 1.0, 9) == "underpowered" and verdict(5.0, None, 50) == "underpowered"


def test_compare_runs_one_row_per_setting(tmp_path):
    w, raw = world(tmp_path)
    run_args = Namespace(only="base,indep,cal-ones,edge-1pt,caps-off,table", group="grp", report_only=False, source=str(raw), rfq_db=str(w["rfq_db"]),
                         books_db=str(w["books_db"]), kalshi_db=str(w["kalshi_db"]), out=str(tmp_path / "cmp.db"), history_db=None,
                         reports_dir=str(tmp_path / "reports"))
    add_print(w["rfq_db"], "r2", direction="BUY", price_e6=990_000, size_e6=500 * E6, at_ms=T0 + 25_000, legs=LEGS)
    c = run_compare(run_args, WindowConfig(start_ms=T0 - 800_000, end_ms=T0 + 400_000))
    names = [r["name"] for r in c["rows"]]
    assert sorted(names) == sorted(["base", "indep", "cal-ones", "edge-1pt", "caps-off", "table"]) and len(names) == 6
    assert c["evaluated_rfqs"] > 0 and (tmp_path / "reports" / "compare-grp.json").exists()
    base = next(r for r in c["rows"] if r["name"] == "base")
    assert "d_model_pnl" not in base and base["filled"] == 1 and base["quoted"] >= 3
    for r in c["rows"]:
        if r["name"] != "base":
            d = r["d_model_pnl"]
            assert d["verdict"] in ("exceeds 2 SE", "within 2 SE", "underpowered") and d["clusters"] >= 1
    # no same-game combos in this world and the print is at 0.99: independence and the raw engine change nothing
    ind = next(r for r in c["rows"] if r["name"] == "indep")
    assert ind["d_model_pnl"]["total"] == pytest.approx(0.0)
    e1 = next(r for r in c["rows"] if r["name"] == "edge-1pt")
    assert e1["gross_markup"] > base["gross_markup"]                                              # a wider margin earns more per fill
    txt = fmt_compare(c)
    assert "base" in txt and "edge-1pt" in txt and "verdict" in txt
    # the group can be re-analysed without replaying
    c2 = run_compare(Namespace(**{**vars(run_args), "report_only": True}), WindowConfig(0, 1))
    assert [r["name"] for r in c2["rows"]] == names
    assert [s[0] for s in SUITE][:2] == ["base", "indep"] and len({s[0] for s in SUITE}) == len(SUITE)


def test_validate_against_a_live_decisions_log(tmp_path):
    from rfqbot.backtest.validate import compare_to_live, raw_completeness
    from rfqbot.quoting.decisions import DecisionLog, decision_row
    from types import SimpleNamespace
    live = DecisionLog(tmp_path / "decisions.db")
    base = dict(rfq_id="", at_ms=T0, direction="BUY", side="YES", requested_unit="notional", requested_e6=1, n_legs=1, legs=[], clusters=[],
                certified=True, p_fair=0.50, u_m=0.01, u_d=0.0, e_b=0.005, e_i=0.0, margin=0.015, maker_action="sell_yes", price_e6=520_000,
                size_e6=100 * E6, decision="quoted", reason="", latency_ms=1.0, model_built_at=None, requester="", condition_id="", deadline_ms=None,
                expires_ms=None, requested_shares=None, kelly_size=None, allowed_size=None, binding_cap=None, headroom=None, d_es1_per_share=None,
                pending_same_combo=None, pending_requesters=None, book=None, edge_segment=None, edge_level=None, edge_source=None, edge_n=None,
                curse_mult=None, info_premium=None, wallet_premium=None)
    for rid, fair, dec in (("a", 0.50, "quoted"), ("b", 0.40, "quoted"), ("c", None, "skipped:leg:cold_game"), ("live_only", 0.3, "quoted")):
        live.write(decision_row(SimpleNamespace(**{**base, "rfq_id": rid, "p_fair": fair, "decision": dec, "at_ms": T0 + 5}), "{}"))
    live.close()
    st = make_store(tmp_path, [drow("a", [leg("a", "cA", "Yes")], fair=0.503, price=0.523), drow("b", [leg("a", "cA", "Yes")], fair=0.46, price=0.48),
                               {**drow("c", [leg("a", "cA", "Yes")]), "decision": "quoted"}, drow("replay_only", [leg("a", "cA", "Yes")])])
    r = compare_to_live(st.conn, "r", tmp_path / "decisions.db")
    assert (r["in_both"], r["only_replay"], r["only_live"], r["decision_kind_agree"]) == (3, 1, 1, 2)
    assert r["p_fair"]["n"] == 2 and r["p_fair"]["max"] == pytest.approx(0.06) and r["p_fair"]["share_within_0.5pt"] == pytest.approx(0.5)
    assert r["decision_matrix"]["live skipped:leg:cold_game | replay quoted"] == 1
    w, raw = world(tmp_path / "w")
    rc = raw_completeness(raw, w["rfq_db"], T0 - 800_000, T0 + 400_000)
    assert rc["raw_unique_requests"] == 7 and rc["raw_duplicates"] == 1 and rc["rfq_db_rfqs"] == 0


def test_game_total_se_splits_cross_game_rows_and_never_joins_dates():
    from rfqbot.backtest.stats import cluster_ids, game_total_se
    games = [["a"], ["b"], ["a", "b"], ["c"]]
    vals = [1.0, 3.0, 2.0, -2.0]
    assert len(set(cluster_ids(games))) == 2                 # union-find fuses a and b
    tot, se, g, gu = game_total_se(vals, games)
    assert tot == pytest.approx(4.0) and g == 3 and gu == 3
    mean = 1.0                                               # centred scores: a=0+.5*... computed by hand
    sa, sb, sc = (1 - mean) + (2 - mean) / 2, (3 - mean) + (2 - mean) / 2, (-2 - mean)
    assert se == pytest.approx((3 / 2 * (sa * sa + sb * sb + sc * sc)) ** 0.5)
    assert game_total_se([1.0], [["a"]])[1] is None
    _, _, ev, uni = game_total_se([1.0, 2.0, 0.0], [["a"], ["b"], ["c"]])
    assert (ev, uni) == (2, 3)                      # the zero-valued game is universe, not evidence


def test_unresolved_fills_are_marked_to_the_reference_run_for_paired_deltas(tmp_path):
    legs_ = [leg("p1", "c1", "Yes")]
    st = BacktestStore(tmp_path / "bt.db")
    for rid, fair in (("base", 0.50), ("alt", 0.56)):
        st.start_run(rid, rid, BacktestConfig().to_json(), json.dumps({}), WindowConfig(0, 1).to_json(), group_id="g")
        st.insert_decisions([drow("q1", legs_, run_id=rid, fair=fair, price=0.52, action="buy_yes", size=100)])
    rfq_db = resolutions_db(tmp_path, [])
    for rid, ref in (("base", None), ("alt", "base")):
        settle_run(st, rid, rfq_db, None, ref_run_id=ref)
    alt = st.conn.execute("SELECT mark, mark_ref, unrealized, unrealized_ref FROM settlements WHERE run_id='alt'").fetchone()
    assert alt["mark"] == pytest.approx(0.56) and alt["mark_ref"] == pytest.approx(0.50)          # own model vs the base run's fair
    assert alt["unrealized"] == pytest.approx(100 * (0.56 - 0.52)) and alt["unrealized_ref"] == pytest.approx(100 * (0.50 - 0.52))
    from rfqbot.backtest.settle import base_run_id
    assert base_run_id(st, "alt") == "base"
