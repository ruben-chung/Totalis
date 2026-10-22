import base64
import hashlib
import hmac
import json

import pytest

from rfqbot.probe.auth import hmac_signature
from rfqbot.probe.config import ProbeConfig, REQUESTER_HOST, load_probe_config
from rfqbot.probe.gateway import DryRunGateway, FakeGateway, HttpGateway, ProbeRequest, parse_response, serialize_body

CFG = ProbeConfig(mode="dry", signer_address="0xdeposit", maker_address="0xdeposit", signature_type=3)
DOC_FIELDS = {"signer_address", "maker_address", "signature_type", "leg_position_ids", "direction", "side", "requested_size"}


def test_dry_run_body_matches_the_docs_field_set():
    gw = DryRunGateway(CFG)
    req = ProbeRequest(("p1", "p2", "p3"), "BUY", "notional", 20_000_000)
    resp = gw.create(req)
    assert resp.status == "DRY_RUN" and resp.price is None and not resp.priced
    b = gw.bodies[0]
    assert b["url"] == REQUESTER_HOST + "/v1/requester/rfq/requests" and b["method"] == "POST"
    assert set(b["body"]) == DOC_FIELDS
    assert b["body"]["side"] == "YES" and b["body"]["signature_type"] == 3 and b["body"]["signer_address"] == "0xdeposit"
    assert b["body"]["requested_size"] == {"unit": "notional", "value_e6": "20000000"}     # value_e6 is a string per the docs
    assert b["body"]["leg_position_ids"] == ["p1", "p2", "p3"]
    assert "POLY_SIGNATURE" in b["headers"] and not any(h.startswith("POLY_BUILDER") for h in b["headers"])
    # the wire serialization is py-clob-client's (json.dumps default separators == str(dict).replace("'", '"'))
    assert req.body_json(CFG) == str(req.body(CFG)).replace("'", '"') == json.dumps(req.body(CFG))
    # builder gateway adds the builder headers and path
    bcfg = ProbeConfig(**{**CFG.__dict__, "gateway": "builder", "gateway_url": "https://gw.example"})
    gw = DryRunGateway(bcfg)
    gw.create(ProbeRequest(("p1", "p2"), "SELL", "shares", 10_000_000))
    assert gw.bodies[0]["url"] == "https://gw.example/v1/builder/rfq/requests" and "POLY_BUILDER_SIGNATURE" in gw.bodies[0]["headers"]


def test_signed_body_serialisation_known_answer():
    """py-clob-client signs ``str(body).replace("'", '"')`` and posts ``json=body`` (requests: json.dumps with
    ", " / ": " separators). Pinned bytes and HMAC for one body so a serializer change cannot pass unnoticed."""
    secret = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()       # throwaway test key
    req = ProbeRequest(("p1", "p2", "p3"), "BUY", "notional", 20_000_000)
    body = req.body(CFG)
    wire = req.body_json(CFG)
    assert wire == ('{"signer_address": "0xdeposit", "maker_address": "0xdeposit", "signature_type": 3, '
                    '"leg_position_ids": ["p1", "p2", "p3"], "direction": "BUY", "side": "YES", '
                    '"requested_size": {"unit": "notional", "value_e6": "20000000"}}')
    assert wire == str(body).replace("'", '"') == serialize_body(body)
    assert wire != json.dumps(body, separators=(",", ":"))                                  # the compact form would not verify
    ts, path = 1773890758, "/v1/requester/rfq/requests"
    expect = base64.urlsafe_b64encode(hmac.new(base64.urlsafe_b64decode(secret), f"{ts}POST{path}{wire}".encode(), hashlib.sha256).digest()).decode()
    assert hmac_signature(secret, ts, "POST", path, wire) == expect == "2aZKjHhzvMag592SMeB7CAVviPJIaHo7vXr6c1SGX74="


def test_http_gateway_requires_live_mode():
    full = dict(poly_address="0xeoa", api_key="k", api_secret="c2VjcmV0", api_passphrase="p", signer_address="0xd", maker_address="0xd")
    for mode in ("off", "dry"):
        with pytest.raises(RuntimeError, match="PROBE_MODE=live"):
            HttpGateway(ProbeConfig(mode=mode, **full), client=object())
    assert HttpGateway(ProbeConfig(mode="live", **full), client=object()).name == "http"


def test_request_validation():
    with pytest.raises(ValueError):
        ProbeRequest(("p1",), "BUY", "notional", 1).body(CFG)                  # fewer than 2 legs
    with pytest.raises(ValueError):
        ProbeRequest(("p1", "p1"), "BUY", "notional", 1).body(CFG)            # duplicate
    with pytest.raises(ValueError):
        ProbeRequest(("p1", "p2"), "BUY", "shares", 1).body(CFG)              # BUY uses notional
    with pytest.raises(ValueError):
        ProbeRequest(("p1", "p2"), "SELL", "notional", 1).body(CFG)           # SELL uses shares
    with pytest.raises(ValueError):
        ProbeRequest(("p1", "p2"), "SELL", "shares", 0).body(CFG)
    with pytest.raises(ValueError):
        ProbeRequest(tuple(f"p{i}" for i in range(51)), "BUY", "notional", 1).body(CFG)


def test_parse_response_shapes():
    ok = parse_response({"rfq_id": "r1", "status": "AWAITING_REQUESTER_ACCEPTANCE", "expires_at": "1773890763000",
                         "request": {"rfq_id": "r1", "requestor_public_id": "req_x", "condition_id": "0xc", "yes_position_id": "y"},
                         "quote": {"quote_id": "q1", "blended_price_e6": "500000"}}, 200, 900.0)
    assert ok.priced and ok.price == 0.5 and ok.requestor_public_id == "req_x" and ok.expires_at == 1773890763000 and ok.quote_id == "q1"
    failed = parse_response({"rfq_id": "r2", "status": "FAILED", "error": {"code": "NO_QUOTES", "message": "no quotes"}}, 200, 5000.0)
    assert failed.status == "FAILED" and not failed.priced and failed.error == "NO_QUOTES: no quotes" and failed.price is None
    http = parse_response({"error": "unauthorized"}, 401, 10.0)
    assert http.status == "ERROR" and http.error == "unauthorized" and http.http_status == 401
    assert parse_response("<html>", 502, 1.0).status == "ERROR"
    assert parse_response({}, 500, 1.0).error == "HTTP 500"


def test_fake_gateway_prices_and_never_accepts():
    gw = FakeGateway(lambda req: 0.25 if req.direction == "BUY" else None, requestor_public_id="req_me", now_ms=lambda: 1_000_000)
    r = gw.create(ProbeRequest(("p1", "p2"), "BUY", "notional", 20_000_000))
    assert r.priced and r.price == 0.25 and r.requestor_public_id == "req_me" and r.expires_at == 1_005_000 and r.rfq_id == "rfq-probe-000001"
    r2 = gw.create(ProbeRequest(("p1", "p2"), "SELL", "shares", 80_000_000))
    assert r2.status == "FAILED" and r2.error.startswith("NO_QUOTES")
    assert gw.accepted == 0 and len(gw.requests) == 2
    assert not hasattr(gw, "accept") and not hasattr(HttpGateway, "accept")


def test_http_gateway_refuses_without_credentials_and_signs_with_them():
    with pytest.raises(RuntimeError) as e:
        HttpGateway(ProbeConfig(mode="live"))
    assert "POLYMARKET_API_PASSPHRASE" in str(e.value) and "POLYMARKET_MAKER_ADDRESS" in str(e.value)

    class Client:
        def __init__(self):
            self.calls = []

        def post(self, url, content, headers):
            self.calls.append((url, content, headers))

            class R:
                status_code = 200
                text = ""

                def json(self):
                    return {"rfq_id": "r9", "status": "FAILED", "error": {"code": "NO_QUOTES", "message": "no quotes"}}
            return R()

    cfg = ProbeConfig(mode="live", poly_address="0xeoa", api_key="k", api_secret="c2VjcmV0", api_passphrase="p",
                      signer_address="0xd", maker_address="0xd")
    c = Client()
    gw = HttpGateway(cfg, client=c)
    resp = gw.create(ProbeRequest(("p1", "p2"), "BUY", "notional", 20_000_000))
    assert resp.status == "FAILED" and resp.rfq_id == "r9"
    url, content, headers = c.calls[0]
    assert url == REQUESTER_HOST + "/v1/requester/rfq/requests"
    assert json.loads(content)["leg_position_ids"] == ["p1", "p2"]
    assert headers["POLY_API_KEY"] == "k" and headers["POLY_PASSPHRASE"] == "p" and len(headers["POLY_SIGNATURE"]) == 44


def test_config_env_and_missing_credentials():
    cfg = load_probe_config(env_file=None, env={"PROBE_MODE": "dry", "PROBE_HORIZONS_SEC": "30,120", "PROBE_MAX_PER_HOUR": "5",
                                                "PROBE_DISGUISE": "off", "POLYMARKET_API_KEY": "k", "POLYMARKET_API_SECRET": "s",
                                                "POLYMARKET_SIGNER_ADDRESS": "0xd"})
    assert cfg.mode == "dry" and cfg.horizons_sec == (30, 120) and cfg.max_per_hour == 5 and cfg.disguise is False
    assert cfg.poly_address == "0xd" and cfg.path == "/v1/requester/rfq/requests"
    assert cfg.missing_credentials() == ["POLYMARKET_API_PASSPHRASE", "POLYMARKET_MAKER_ADDRESS"]
    bcfg = load_probe_config(env_file=None, env={"PROBE_GATEWAY": "builder"})
    assert any(k.startswith("PROBE_GATEWAY_URL") for k in bcfg.missing_credentials()) and "PROBE_BUILDER_SECRET" in bcfg.missing_credentials()
    with pytest.raises(ValueError):
        load_probe_config(env_file=None, env={"PROBE_MODE": "yes"})
