"""Requester-side RFQ gateway client behind an interface: Http (live), DryRun (logs the body), Fake (tests).

The create call blocks until the quote competition closes and returns the single winning quote
(``status: AWAITING_REQUESTER_ACCEPTANCE``, ``expires_at`` ms) or HTTP 200 ``status: FAILED`` with an
error (``NO_QUOTES``). We never call accept: letting ``expires_at`` pass is the "no". Nothing in this
module signs orders.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..slugs import E6
from .auth import headers_for, redact
from .config import ProbeConfig

log = logging.getLogger(__name__)
SIDE_YES = "YES"


@dataclass(frozen=True)
class ProbeRequest:
    leg_position_ids: tuple[str, ...]
    direction: str                 # BUY | SELL (requester's direction)
    unit: str                      # notional (BUY) | shares (SELL)
    value_e6: int

    def body(self, cfg: ProbeConfig) -> dict[str, Any]:
        if self.direction not in ("BUY", "SELL"):
            raise ValueError(f"direction {self.direction!r}")
        if (self.direction == "BUY") != (self.unit == "notional"):
            raise ValueError("BUY uses notional, SELL uses shares")
        if not 2 <= len(self.leg_position_ids) <= 50 or len(set(self.leg_position_ids)) != len(self.leg_position_ids):
            raise ValueError("2 to 50 unique leg position ids")
        if self.value_e6 <= 0:
            raise ValueError("requested size must be positive")
        return {"signer_address": cfg.signer_address, "maker_address": cfg.maker_address, "signature_type": cfg.signature_type,
                "leg_position_ids": list(self.leg_position_ids), "direction": self.direction, "side": SIDE_YES,
                "requested_size": {"unit": self.unit, "value_e6": str(self.value_e6)}}

    def body_json(self, cfg: ProbeConfig) -> str:
        """The exact wire serialization (the signature covers these bytes). py-clob-client signs
        ``str(body).replace("'", '"')`` and posts ``json=body`` through requests, which is ``json.dumps``
        with its default separators (", " and ": ") in insertion order; for a body of strings, ints,
        lists and dicts (ours) the two are byte-identical, so that is what we sign and send as
        ``content=`` (an httpx ``json=`` would be compact and the venue would reject the signature)."""
        return serialize_body(self.body(cfg))


def serialize_body(body: dict[str, Any]) -> str:
    """py-clob-client's signed-body form: ``json.dumps`` default separators == ``str(dict).replace("'", '"')``."""
    return json.dumps(body, separators=(", ", ": "))


@dataclass
class ProbeResponse:
    status: str                              # AWAITING_REQUESTER_ACCEPTANCE | FAILED | DRY_RUN | ERROR
    rfq_id: str | None = None
    requestor_public_id: str | None = None
    condition_id: str | None = None
    yes_position_id: str | None = None
    blended_price_e6: int | None = None
    expires_at: int | None = None
    quote_id: str | None = None
    error: str | None = None
    http_status: int | None = None
    latency_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def price(self) -> float | None:
        return None if self.blended_price_e6 is None else self.blended_price_e6 / E6

    @property
    def priced(self) -> bool:
        return self.status == "AWAITING_REQUESTER_ACCEPTANCE" and self.blended_price_e6 is not None


def parse_response(data: Any, http_status: int, latency_ms: float) -> ProbeResponse:
    if not isinstance(data, dict):
        return ProbeResponse("ERROR", error=f"non-object response: {str(data)[:200]}", http_status=http_status, latency_ms=latency_ms)
    status = str(data.get("status") or ("ERROR" if http_status >= 400 else "UNKNOWN")).upper()
    req = data.get("request") or {}
    q = data.get("quote") or {}
    err = data.get("error")
    if isinstance(err, dict):
        err = f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
    elif err is not None:
        err = str(err)
    if http_status >= 400 and not err:
        err = f"HTTP {http_status}"
    bp = q.get("blended_price_e6")
    return ProbeResponse(status=status, rfq_id=data.get("rfq_id") or req.get("rfq_id"), requestor_public_id=req.get("requestor_public_id"),
                         condition_id=req.get("condition_id"), yes_position_id=req.get("yes_position_id"),
                         blended_price_e6=int(bp) if bp not in (None, "") else None,
                         expires_at=int(data["expires_at"]) if data.get("expires_at") not in (None, "") else None,
                         quote_id=q.get("quote_id"), error=err, http_status=http_status, latency_ms=latency_ms, raw=data)


class Gateway(Protocol):
    name: str

    def create(self, req: ProbeRequest) -> ProbeResponse: ...


class HttpGateway:
    """POST {gateway_url}{path} with the account (and builder) headers. Accept is intentionally absent.
    Constructs only under PROBE_MODE=live with the full credential set."""
    name = "http"

    def __init__(self, cfg: ProbeConfig, client: Any | None = None):
        self.cfg = cfg
        if cfg.mode != "live":
            raise RuntimeError(f"HttpGateway needs PROBE_MODE=live (mode is {cfg.mode!r}); use DryRunGateway")
        missing = cfg.missing_credentials()
        if missing:
            raise RuntimeError("probe gateway credentials missing: " + ", ".join(missing))
        if client is None:
            import httpx
            client = httpx.Client(timeout=cfg.timeout_sec)
        self.client = client

    def create(self, req: ProbeRequest) -> ProbeResponse:
        body = req.body_json(self.cfg)
        headers = headers_for(self.cfg, "POST", self.cfg.path, body)
        t0 = time.perf_counter()
        try:
            r = self.client.post(self.cfg.gateway_url + self.cfg.path, content=body.encode(), headers=headers)
            latency = (time.perf_counter() - t0) * 1000
            try:
                data = r.json()
            except Exception:
                data = {"status": "ERROR", "error": r.text[:300]}
            resp = parse_response(data, r.status_code, latency)
        except Exception as e:
            resp = ProbeResponse("ERROR", error=f"{type(e).__name__}: {e}", latency_ms=(time.perf_counter() - t0) * 1000)
        log.info("probe %s %s legs=%d -> %s price=%s err=%s (%.0f ms) headers=%s", req.direction, req.unit, len(req.leg_position_ids),
                 resp.status, resp.price, resp.error, resp.latency_ms, redact(headers))
        return resp


class DryRunGateway:
    """Builds the exact body and headers shape, sends nothing."""
    name = "dry"

    def __init__(self, cfg: ProbeConfig):
        self.cfg = cfg
        self.bodies: list[dict[str, Any]] = []

    def create(self, req: ProbeRequest) -> ProbeResponse:
        body = req.body_json(self.cfg)
        self.bodies.append({"url": self.cfg.gateway_url + self.cfg.path, "method": "POST", "body": json.loads(body),
                            "headers": sorted(headers_for(self.cfg, "POST", self.cfg.path, body, timestamp=0).keys())})
        return ProbeResponse("DRY_RUN", raw={"body": json.loads(body)})


class FakeGateway:
    """Answers from ``price_fn(req) -> float | None`` (None = no quotes). For tests and the analysis demo."""
    name = "fake"

    def __init__(self, price_fn: Callable[[ProbeRequest], float | None], *, requestor_public_id: str = "req_probe_fake",
                 latency_ms: float = 1200.0, now_ms: Callable[[], int] | None = None):
        self.price_fn = price_fn
        self.requestor_public_id = requestor_public_id
        self.latency_ms = latency_ms
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self.requests: list[ProbeRequest] = []
        self.accepted = 0   # must stay 0: nothing here accepts

    def create(self, req: ProbeRequest) -> ProbeResponse:
        self.requests.append(req)
        n = len(self.requests)
        p = self.price_fn(req)
        rid = f"rfq-probe-{n:06d}"
        if p is None:
            return parse_response({"rfq_id": rid, "status": "FAILED", "error": {"code": "NO_QUOTES", "message": "no quotes"}}, 200, self.latency_ms)
        now = self._now()
        data = {"rfq_id": rid, "status": "AWAITING_REQUESTER_ACCEPTANCE", "expires_at": now + 5000,
                "request": {"rfq_id": rid, "requestor_public_id": self.requestor_public_id, "leg_position_ids": list(req.leg_position_ids),
                            "condition_id": "0x" + f"{abs(hash(req.leg_position_ids)):x}".rjust(64, "0")[:64], "yes_position_id": f"yes-{n}",
                            "no_position_id": f"no-{n}", "direction": req.direction, "side": SIDE_YES,
                            "requested_size": {"unit": req.unit, "value_e6": str(req.value_e6)}, "created_at": now - int(self.latency_ms)},
                "quote": {"quote_id": f"q-{n}", "blended_price_e6": str(int(round(p * E6))), "maker_amount_e6": "0", "taker_amount_e6": "0",
                          "total_required_e6": "0", "net_receive_e6": "0"}}
        return parse_response(data, 200, self.latency_ms)
