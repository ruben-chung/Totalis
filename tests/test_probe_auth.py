import base64
import hashlib
import hmac

from rfqbot.probe.auth import account_headers, builder_headers, decode_secret, headers_for, hmac_signature, redact
from rfqbot.probe.config import ProbeConfig

SECRET = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()   # 32-byte key, base64url


def test_signature_against_fixed_vector():
    # independent computation of the docs' recipe: HMAC-SHA256(base64-decoded secret, ts + METHOD + path + body), base64url padded
    ts, method, path, body = 1773890758, "POST", "/v1/requester/rfq/requests", '{"a":1}'
    expect = base64.urlsafe_b64encode(hmac.new(base64.urlsafe_b64decode(SECRET), f"{ts}POST{path}{body}".encode(), hashlib.sha256).digest()).decode()
    assert hmac_signature(SECRET, ts, "post", path, body) == expect
    # pinned regression vector for this key / message
    assert hmac_signature(SECRET, ts, method, path, body) == "MMBtYuCim4QL15vhYR-_RT3OkJYENP7t3LoFdmWNbTA="
    assert hmac_signature(SECRET, ts, "GET", path) != hmac_signature(SECRET, ts, "POST", path)
    assert hmac_signature(SECRET, ts, method, path, body).endswith("=")          # padding preserved


def test_secret_decoding_accepts_both_alphabets_and_missing_padding():
    raw = bytes(range(0, 48))
    std = base64.b64encode(raw).decode()
    url = base64.urlsafe_b64encode(raw).decode()
    assert decode_secret(std) == raw and decode_secret(url) == raw and decode_secret(url.rstrip("=")) == raw


def test_header_sets_and_redaction():
    cfg = ProbeConfig(poly_address="0xabc", api_key="k", api_secret=SECRET, api_passphrase="p",
                      builder_api_key="bk", builder_passphrase="bp", builder_secret=SECRET, gateway="builder")
    acc = account_headers(cfg, "POST", "/v1/builder/rfq/requests", "{}", timestamp=5)
    assert set(acc) == {"POLY_ADDRESS", "POLY_API_KEY", "POLY_PASSPHRASE", "POLY_TIMESTAMP", "POLY_SIGNATURE"}
    assert acc["POLY_TIMESTAMP"] == "5" and acc["POLY_ADDRESS"] == "0xabc"
    b = builder_headers(cfg, "POST", "/v1/builder/rfq/requests", "{}", timestamp=5)
    assert set(b) == {"POLY_BUILDER_API_KEY", "POLY_BUILDER_PASSPHRASE", "POLY_BUILDER_TIMESTAMP", "POLY_BUILDER_SIGNATURE"}
    all_h = headers_for(cfg, "POST", "/v1/builder/rfq/requests", "{}", timestamp=5)
    assert set(all_h) == set(acc) | set(b) | {"Content-Type"}
    req_only = headers_for(ProbeConfig(**{**cfg.__dict__, "gateway": "requester"}), "POST", "/v1/requester/rfq/requests", "{}", timestamp=5)
    assert not any(k.startswith("POLY_BUILDER") for k in req_only)
    r = redact(all_h)
    assert r["POLY_SIGNATURE"] == "***" and r["POLY_PASSPHRASE"] == "***" and r["POLY_TIMESTAMP"] == "5"
    j = ProbeConfig(api_secret="zq9secret", api_passphrase="pw7pass").to_json()
    assert "***" in j and "zq9secret" not in j and "pw7pass" not in j
