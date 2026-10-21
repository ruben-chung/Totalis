"""Polymarket CLOB L2 / builder HMAC headers (docs.polymarket.com/trading/combos/requesters and /builders).

signature = base64url(HMAC_SHA256(base64-decoded secret, timestamp + METHOD + path + body)), padding
preserved; timestamp in Unix seconds; path without host or query; body exactly as sent (omitted for
GET). Same recipe for the builder headers with the builder secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from .config import ProbeConfig

ACCOUNT_HEADERS = ("POLY_ADDRESS", "POLY_API_KEY", "POLY_PASSPHRASE", "POLY_TIMESTAMP", "POLY_SIGNATURE")
BUILDER_HEADERS = ("POLY_BUILDER_API_KEY", "POLY_BUILDER_PASSPHRASE", "POLY_BUILDER_TIMESTAMP", "POLY_BUILDER_SIGNATURE")


def decode_secret(secret: str) -> bytes:
    """Secrets are base64 (the CLOB hands out URL-safe ones); accept both alphabets, pad if needed."""
    s = secret.strip()
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def hmac_signature(secret: str, timestamp: int | str, method: str, path: str, body: str = "") -> str:
    msg = f"{timestamp}{method.upper()}{path}{body}".encode()
    digest = hmac.new(decode_secret(secret), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode()


def account_headers(cfg: ProbeConfig, method: str, path: str, body: str = "", timestamp: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if timestamp is None else int(timestamp)
    return {"POLY_ADDRESS": cfg.poly_address, "POLY_API_KEY": cfg.api_key, "POLY_PASSPHRASE": cfg.api_passphrase,
            "POLY_TIMESTAMP": str(ts), "POLY_SIGNATURE": hmac_signature(cfg.api_secret, ts, method, path, body)}


def builder_headers(cfg: ProbeConfig, method: str, path: str, body: str = "", timestamp: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if timestamp is None else int(timestamp)
    return {"POLY_BUILDER_API_KEY": cfg.builder_api_key, "POLY_BUILDER_PASSPHRASE": cfg.builder_passphrase,
            "POLY_BUILDER_TIMESTAMP": str(ts), "POLY_BUILDER_SIGNATURE": hmac_signature(cfg.builder_secret, ts, method, path, body)}


def headers_for(cfg: ProbeConfig, method: str, path: str, body: str = "", timestamp: int | None = None) -> dict[str, str]:
    h = {"Content-Type": "application/json", **account_headers(cfg, method, path, body, timestamp)}
    if cfg.gateway == "builder":
        h.update(builder_headers(cfg, method, path, body, timestamp))
    return h


def redact(headers: dict[str, str]) -> dict[str, str]:
    out = {}
    for k, v in headers.items():
        out[k] = v if k in ("Content-Type", "POLY_TIMESTAMP", "POLY_BUILDER_TIMESTAMP", "POLY_ADDRESS") else ("***" if v else "")
    return out
