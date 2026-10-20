"""Exercise RfqSocket + Listener against a local mock gateway."""
import asyncio
import json
import time

import pytest
from websockets.asyncio.server import serve

from rfqbot import messages as m
from rfqbot.config import Settings
from rfqbot.listener import Listener
from rfqbot.normalizer import PaperQuote
from rfqbot.store import Store
from rfqbot.rawlog import RawLog, iter_rawlog_dir
from rfqbot.ws_client import AuthError, AuthResult, Connected, Disconnected, FatalGatewayError, Frame, RfqSocket

AUTH = {"type": "auth", "auth": {"apiKey": "k", "secret": "s", "passphrase": "p"},
        "identity": {"signer_address": "0xS", "maker_address": "0xM", "signature_type": 3}}


def _settings(url: str, **kw) -> Settings:
    return Settings(api_key="k", api_secret="s", api_passphrase="p", signer_address="0xS", maker_address="0xM",
                    signature_type=3, ws_url=url, expiry_grace_ms=100, **kw)


def _rfq(i: int) -> dict:
    # deadline well in the future: a past deadline lets the 50 ms expiry sweeper expire the RFQ
    # between its quote and its execution update, which made the end-to-end test flaky under load
    deadline = int(time.time() * 1000) + 60_000
    return {"type": "RFQ_REQUEST", "rfq_id": f"rfq_{i}", "requestor_public_id": "r",
            "leg_position_ids": ["a", "b"], "condition_id": "0xc", "yes_position_id": "y", "no_position_id": "n",
            "direction": "BUY", "side": "YES", "requested_size": {"unit": "notional", "value_e6": "1000000"},
            "submission_deadline": str(deadline)}


class MockGateway:
    """Scriptable gateway. `script` is a list of actions per connection index."""

    def __init__(self):
        self.connections = 0
        self.auth_payloads = []
        self.pongs = []
        self.script = []
        self.expect_auth = True

    async def handler(self, ws):
        idx = self.connections
        self.connections += 1
        if self.expect_auth:
            first = json.loads(await ws.recv())
            self.auth_payloads.append(first)
        actions = self.script[idx] if idx < len(self.script) else self.script[-1]
        for act in actions:
            kind = act[0]
            if kind == "auth_ok":
                await ws.send(json.dumps({"type": "auth", "success": True, "address": "0xM", "role": "maker"}))
            elif kind == "auth_fail":
                await ws.send(json.dumps({"type": "auth", "success": False, "error": "unauthenticated"}))
                await ws.close()
                return
            elif kind == "send":
                await ws.send(json.dumps(act[1]))
            elif kind == "ping":
                pong_waiter = await ws.ping(b"rfq")
                await asyncio.wait_for(pong_waiter, 2)
                self.pongs.append(b"rfq")
            elif kind == "drop":
                await ws.close(code=1012, reason="service restart")
                return
            elif kind == "sleep":
                await asyncio.sleep(act[1])
            elif kind == "hold":
                await ws.wait_closed()
                return


@pytest.fixture
async def gateway():
    gw = MockGateway()
    async with serve(gw.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        gw.url = f"ws://127.0.0.1:{port}/ws/rfq"
        yield gw


async def test_auth_frames_and_pong(gateway):
    gateway.script = [[("auth_ok",), ("ping",), ("send", _rfq(1)), ("drop",)]]
    sock = RfqSocket(gateway.url, AUTH, backoff_base_s=0.01, backoff_cap_s=0.02)
    events = []
    async for ev in sock.events():
        events.append(ev)
        if isinstance(ev, Disconnected):
            sock.stop()
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["Connected", "Frame", "AuthResult", "Frame", "Disconnected"]
    assert events[0].auth_sent is True
    assert events[2].ok and events[2].role == "maker"
    assert gateway.auth_payloads[0] == AUTH
    assert gateway.pongs == [b"rfq"]
    assert json.loads(events[3].text)["rfq_id"] == "rfq_1"
    assert events[4].code == 1012


async def test_reconnects_after_drop_with_new_conn_id(gateway):
    gateway.script = [
        [("auth_ok",), ("send", _rfq(1)), ("drop",)],
        [("auth_ok",), ("send", _rfq(2)), ("hold",)],
    ]
    sock = RfqSocket(gateway.url, AUTH, backoff_base_s=0.01, backoff_cap_s=0.02)
    conns, rfqs = [], []
    async for ev in sock.events():
        if isinstance(ev, Connected):
            conns.append(ev.conn_id)
        if isinstance(ev, Frame) and '"RFQ_REQUEST"' in ev.text:
            rfqs.append(json.loads(ev.text)["rfq_id"])
            if len(rfqs) == 2:
                sock.stop()
    assert len(conns) == 2 and conns[0] != conns[1]
    assert rfqs == ["rfq_1", "rfq_2"]
    assert gateway.connections == 2


async def test_auth_failure_gives_up_after_n_when_required(gateway):
    gateway.script = [[("auth_fail",)]]
    sock = RfqSocket(gateway.url, AUTH, require_auth=True, backoff_base_s=0.01, backoff_cap_s=0.02, max_auth_failures=3)
    fails = 0
    with pytest.raises(AuthError):
        async for ev in sock.events():
            if isinstance(ev, AuthResult) and not ev.ok:
                fails += 1
    assert fails == 3 and gateway.connections == 3


async def test_auth_failure_is_tolerated_in_paper_mode(gateway):
    # Real gateway behaviour: auth reply arrives mid-stream, feed continues regardless.
    gateway.script = [[("send", _rfq(1)), ("auth_fail",)], [("send", _rfq(2)), ("hold",)]]
    sock = RfqSocket(gateway.url, AUTH, backoff_base_s=0.01, backoff_cap_s=0.02)
    seen = []
    async for ev in sock.events():
        seen.append(ev)
        if isinstance(ev, Frame) and "rfq_2" in ev.text:
            sock.stop()
    assert [e.ok for e in seen if isinstance(e, AuthResult)] == [False]
    assert gateway.connections == 2


async def test_unauthenticated_connect_streams(gateway):
    gateway.expect_auth = False
    gateway.script = [[("send", _rfq(7)), ("hold",)]]
    sock = RfqSocket(gateway.url, None, backoff_base_s=0.01)
    got = []
    async for ev in sock.events():
        if isinstance(ev, Connected):
            assert ev.auth_sent is False
        if isinstance(ev, Frame):
            got.append(json.loads(ev.text)["rfq_id"]); sock.stop()
    assert got == ["rfq_7"] and gateway.auth_payloads == []


async def test_idle_watchdog_reconnects(gateway):
    gateway.script = [[("auth_ok",), ("hold",)], [("auth_ok",), ("send", _rfq(9)), ("hold",)]]
    sock = RfqSocket(gateway.url, AUTH, idle_timeout_s=0.2, ping_interval_s=None,
                     backoff_base_s=0.01, backoff_cap_s=0.02)
    seen = []
    async for ev in sock.events():
        seen.append(ev)
        if isinstance(ev, Frame) and "rfq_9" in ev.text:
            sock.stop()
    d = [e for e in seen if isinstance(e, Disconnected)]
    assert d and "idle" in d[0].reason
    assert gateway.connections == 2


async def test_listener_end_to_end_with_paper_quote_and_fatal(gateway, tmp_path):
    settled = {
        "type": "RFQ_EXECUTION_UPDATE", "rfq_id": "rfq_1", "status": "CONFIRMED", "tx_hash": "0xabc",
    }
    gateway.script = [
        [("auth_ok",), ("send", _rfq(1)), ("drop",)],
        [("auth_ok",), ("send", settled), ("send", _rfq(2)),
         ("send", {"type": "RFQ_ERROR", "request_type": "RFQ_QUOTE", "code": "UNAUTHORIZED_ROLE", "error": "nope"}),
         ("hold",)],
    ]
    store = Store(None)
    settings = _settings(gateway.url, raw_db_types="all")
    sock = RfqSocket(gateway.url, AUTH, backoff_base_s=0.01, backoff_cap_s=0.02)
    rawlog = RawLog(tmp_path / "raw", flush_s=0)

    def handler(rfq: m.RfqRequest, _s: Store):
        return PaperQuote(price_e6=450_000, size_e6=2_000_000, reason="test")

    lst = Listener(settings, store, on_rfq=handler, socket=sock, catalog=None, rawlog=rawlog, sweep_interval_s=0.05)
    with pytest.raises(FatalGatewayError):
        await asyncio.wait_for(lst.run(), 5)

    # raw frames: 2 auth + 2 rfq + 1 exec + 1 error inbound, 2 paper quotes outbound
    assert store.scalar("SELECT COUNT(*) FROM raw_messages WHERE direction='in'") == 6
    logged = list(iter_rawlog_dir(tmp_path / "raw"))
    assert len(logged) == 8 and sum(1 for l in logged if l["d"] == "out") == 2
    assert json.loads(logged[1]["p"])["rfq_id"] == "rfq_1"
    assert store.scalar("SELECT COUNT(*) FROM raw_messages WHERE direction='out' AND msg_type='RFQ_QUOTE'") == 2
    assert store.scalar("SELECT COUNT(*) FROM connections") == 2
    assert store.scalar("SELECT COUNT(*) FROM connections WHERE auth_ok=1") == 2
    assert store.scalar("SELECT close_code FROM connections ORDER BY connected_at_ms LIMIT 1") == 1012
    assert store.get_rfq("rfq_1")["status"] == "SETTLED"
    assert store.scalar("SELECT tx_hash FROM settlements WHERE rfq_id='rfq_1'") == "0xabc"
    assert store.latest_quote("rfq_1")["state"] == "FILLED"
    assert store.latest_quote("rfq_1")["is_paper"] == 1
    # rfq_2: quote recorded, then rejected by the fatal error (request_type RFQ_QUOTE, no rfq_id -> event-less)
    assert store.latest_quote("rfq_2")["state"] in ("SUBMITTED", "REJECTED")
    assert "UNAUTHORIZED_ROLE" in (lst.fatal or "")


def test_rawlog_rotates_hourly_and_survives_truncation(tmp_path):
    rl = RawLog(tmp_path / "raw", flush_s=0)
    h1 = 1_760_000_000_000               # some hour
    h2 = h1 + 3_600_000                  # next hour
    rl.write(at_ms=h1, direction="in", conn_id="c1", seq=1, text='{"type":"X","n":1}')
    rl.write(at_ms=h1 + 1, direction="in", conn_id="c1", seq=2, text='{"type":"X","n":2}')
    rl.write(at_ms=h2, direction="in", conn_id="c1", seq=3, text='{"type":"X","n":3}')
    rl.close()
    files = sorted(p.relative_to(tmp_path / "raw").as_posix() for p in (tmp_path / "raw").rglob("*.gz"))
    assert len(files) == 2 and files[0].endswith(".jsonl.gz")
    rows = list(iter_rawlog_dir(tmp_path / "raw"))
    assert [json.loads(r["p"])["n"] for r in rows] == [1, 2, 3]
    # A restart within the same hour appends a second gzip member. Simulate a crash by
    # chopping the file's tail: everything flushed before the cut must still be readable.
    rl2 = RawLog(tmp_path / "raw", flush_s=0)
    rl2.write(at_ms=h2 + 5, direction="out", conn_id=None, seq=None, text='{"type":"Y"}')
    rl2.flush()
    for i in range(50):
        rl2.write(at_ms=h2 + 10 + i, direction="in", conn_id="c2", seq=i, text='{"type":"Z","n":%d}' % i)
    rl2.close()
    path = rl2.path_for(h2)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - 40])
    rows = list(iter_rawlog_dir(tmp_path / "raw"))
    kinds = [json.loads(r["p"])["type"] for r in rows]
    assert kinds[:4] == ["X", "X", "X", "Y"] and kinds.count("Z") < 50


async def test_malformed_frame_does_not_kill_listener(gateway):
    bad = {"type": "RFQ_REQUEST", "rfq_id": "bad", "requested_size": {"unit": "notional", "value_e6": "1.5e6"},
           "submission_deadline": "soon"}
    gateway.script = [[("auth_ok",), ("send", bad), ("send", {"type": "RFQ_UNMET_DEMAND_SUMMARY", "rfqs": ["x"]}),
                       ("send", _rfq(1)), ("hold",)]]
    store = Store(None)
    sock = RfqSocket(gateway.url, AUTH, backoff_base_s=0.01, backoff_cap_s=0.02)
    lst = Listener(_settings(gateway.url), store, socket=sock, catalog=None, rawlog=None, sweep_interval_s=0.05)

    async def stop_when_done():
        while store.get_rfq("rfq_1") is None:
            await asyncio.sleep(0.02)
        lst.stop()

    await asyncio.wait_for(asyncio.gather(lst.run(), stop_when_done()), 5)
    assert lst.counts.get("normalize_errors") == 2
    assert store.get_rfq("rfq_1")["status"] == "RECEIVED"
    assert store.scalar("SELECT COUNT(*) FROM raw_messages WHERE msg_type='RFQ_UNMET_DEMAND_SUMMARY'") == 1
