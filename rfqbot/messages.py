"""Typed parsing of Combos RFQ gateway messages.

Wire format reference: docs.polymarket.com/asyncapi-rfq.json. Every parser is
tolerant: unknown fields are ignored, unknown types become RawMessage so the
raw frame is never lost.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

Direction = Literal["BUY", "SELL"]

# Inbound (server -> client)
RFQ_REQUEST = "RFQ_REQUEST"
ACK_RFQ_QUOTE = "ACK_RFQ_QUOTE"
ACK_RFQ_QUOTE_CANCEL = "ACK_RFQ_QUOTE_CANCEL"
RFQ_CONFIRMATION_REQUEST = "RFQ_CONFIRMATION_REQUEST"
ACK_RFQ_CONFIRMATION_RESPONSE = "ACK_RFQ_CONFIRMATION_RESPONSE"
RFQ_EXECUTION_UPDATE = "RFQ_EXECUTION_UPDATE"
RFQ_TRADE = "RFQ_TRADE"
RFQ_ERROR = "RFQ_ERROR"
RFQ_UNMET_DEMAND_SUMMARY = "RFQ_UNMET_DEMAND_SUMMARY"
AUTH = "auth"

# Outbound (client -> server)
RFQ_QUOTE = "RFQ_QUOTE"
RFQ_QUOTE_CANCEL = "RFQ_QUOTE_CANCEL"
RFQ_CONFIRMATION_RESPONSE = "RFQ_CONFIRMATION_RESPONSE"

TERMINAL_EXECUTION_STATUSES = {"CONFIRMED", "FAILED"}
# RFQ_ERROR codes after which the gateway closes the socket and retrying is pointless.
FATAL_ERROR_CODES = {"UNAUTHORIZED_ROLE", "ADDRESS_MISMATCH"}


class ParseError(ValueError):
    pass


def _s(d: dict, key: str, default: str = "") -> str:
    v = d.get(key, default)
    return "" if v is None else str(v)


def _i(d: dict, key: str, default: int = 0) -> int:
    v = d.get(key, default)
    if v in (None, ""):
        return default
    return int(v)


@dataclass(frozen=True)
class RequestedSize:
    unit: str          # "notional" (pUSD, for BUY) | "shares" (for SELL)
    value_e6: int      # 6-decimal fixed point


@dataclass(frozen=True)
class AuthResponse:
    type: str
    success: bool
    address: str = ""
    role: str = ""
    error: str = ""


@dataclass(frozen=True)
class RfqRequest:
    type: str
    rfq_id: str
    requestor_public_id: str
    leg_position_ids: tuple[str, ...]
    condition_id: str
    yes_position_id: str
    no_position_id: str
    direction: str
    side: str
    requested_size: RequestedSize
    submission_deadline_ms: int


@dataclass(frozen=True)
class AckQuote:
    type: str
    rfq_id: str
    quote_id: str


@dataclass(frozen=True)
class AckQuoteCancel:
    type: str
    rfq_id: str
    quote_id: str


@dataclass(frozen=True)
class ConfirmationRequest:
    type: str
    rfq_id: str
    quote_id: str
    signer_address: str
    maker_address: str
    signature_type: int
    leg_position_ids: tuple[str, ...]
    condition_id: str
    yes_position_id: str
    no_position_id: str
    direction: str
    side: str
    fill_size_e6: int
    price_e6: int
    confirm_by_ms: int


@dataclass(frozen=True)
class AckConfirmationResponse:
    type: str
    rfq_id: str
    quote_id: str
    decision: str


@dataclass(frozen=True)
class ExecutionUpdate:
    type: str
    rfq_id: str
    status: str          # MATCHED | MINED | RETRYING | CONFIRMED | FAILED
    tx_hash: str = ""
    quote_id: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_EXECUTION_STATUSES


@dataclass(frozen=True)
class TradeBroadcast:
    type: str
    rfq_id: str
    requester_id: str
    condition_id: str
    leg_position_ids: tuple[str, ...]
    direction: str
    side: str
    price_e6: int
    size_e6: int
    executed_at_ms: int


@dataclass(frozen=True)
class RfqError:
    type: str
    request_type: str
    rfq_id: str
    quote_id: str
    code: str
    error: str

    @property
    def fatal(self) -> bool:
        return self.code in FATAL_ERROR_CODES


@dataclass(frozen=True)
class UnmetDemandItem:
    reason: str
    direction: str
    side: str
    leg_position_ids: tuple[str, ...]
    condition_id: str
    bucket_unit: str
    min_value_e6: int
    max_value_e6: int
    count: int


@dataclass(frozen=True)
class UnmetDemandSummary:
    """Periodic gateway broadcast (observed, undocumented): combos that went unfilled in the last window."""
    type: str
    window_ms: int
    total_unfilled: int
    items: tuple[UnmetDemandItem, ...]


@dataclass(frozen=True)
class RawMessage:
    """Anything we did not recognise. Kept so nothing is dropped."""
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


Inbound = (
    AuthResponse | RfqRequest | AckQuote | AckQuoteCancel | ConfirmationRequest
    | AckConfirmationResponse | ExecutionUpdate | TradeBroadcast | RfqError | UnmetDemandSummary | RawMessage
)


def _legs(d: dict) -> tuple[str, ...]:
    legs = d.get("leg_position_ids") or []
    return tuple(str(x) for x in legs)


def parse_inbound(obj: dict[str, Any]) -> Inbound:
    if not isinstance(obj, dict):
        raise ParseError(f"message is not an object: {type(obj).__name__}")
    t = _s(obj, "type")
    if not t:
        raise ParseError("message has no 'type'")

    if t == AUTH:
        return AuthResponse(
            type=t, success=bool(obj.get("success")),
            address=_s(obj, "address"), role=_s(obj, "role"), error=_s(obj, "error"),
        )
    if t == RFQ_REQUEST:
        rs = obj.get("requested_size") or {}
        return RfqRequest(
            type=t,
            rfq_id=_s(obj, "rfq_id"),
            requestor_public_id=_s(obj, "requestor_public_id"),
            leg_position_ids=_legs(obj),
            condition_id=_s(obj, "condition_id"),
            yes_position_id=_s(obj, "yes_position_id"),
            no_position_id=_s(obj, "no_position_id"),
            direction=_s(obj, "direction").upper(),
            side=_s(obj, "side").upper() or "YES",
            requested_size=RequestedSize(unit=_s(rs, "unit"), value_e6=_i(rs, "value_e6")),
            submission_deadline_ms=_i(obj, "submission_deadline"),
        )
    if t == ACK_RFQ_QUOTE:
        return AckQuote(type=t, rfq_id=_s(obj, "rfq_id"), quote_id=_s(obj, "quote_id"))
    if t == ACK_RFQ_QUOTE_CANCEL:
        return AckQuoteCancel(type=t, rfq_id=_s(obj, "rfq_id"), quote_id=_s(obj, "quote_id"))
    if t == RFQ_CONFIRMATION_REQUEST:
        return ConfirmationRequest(
            type=t,
            rfq_id=_s(obj, "rfq_id"),
            quote_id=_s(obj, "quote_id"),
            signer_address=_s(obj, "signer_address"),
            maker_address=_s(obj, "maker_address"),
            signature_type=_i(obj, "signature_type"),
            leg_position_ids=_legs(obj),
            condition_id=_s(obj, "condition_id"),
            yes_position_id=_s(obj, "yes_position_id"),
            no_position_id=_s(obj, "no_position_id"),
            direction=_s(obj, "direction").upper(),
            side=_s(obj, "side").upper() or "YES",
            fill_size_e6=_i(obj, "fill_size_e6"),
            price_e6=_i(obj, "price_e6"),
            confirm_by_ms=_i(obj, "confirm_by"),
        )
    if t == ACK_RFQ_CONFIRMATION_RESPONSE:
        return AckConfirmationResponse(
            type=t, rfq_id=_s(obj, "rfq_id"), quote_id=_s(obj, "quote_id"), decision=_s(obj, "decision"),
        )
    if t == RFQ_EXECUTION_UPDATE:
        return ExecutionUpdate(
            type=t, rfq_id=_s(obj, "rfq_id"), status=_s(obj, "status").upper(),
            tx_hash=_s(obj, "tx_hash"), quote_id=_s(obj, "quote_id"),
        )
    if t == RFQ_TRADE:
        return TradeBroadcast(
            type=t,
            rfq_id=_s(obj, "rfq_id"),
            requester_id=_s(obj, "requester_id"),
            condition_id=_s(obj, "condition_id"),
            leg_position_ids=_legs(obj),
            direction=_s(obj, "direction").upper(),
            side=_s(obj, "side").upper() or "YES",
            price_e6=_i(obj, "price_e6"),
            size_e6=_i(obj, "size_e6"),
            executed_at_ms=_i(obj, "executed_at"),
        )
    if t == RFQ_ERROR:
        return RfqError(
            type=t,
            request_type=_s(obj, "request_type"),
            rfq_id=_s(obj, "rfq_id"),
            quote_id=_s(obj, "quote_id"),
            code=_s(obj, "code").upper(),
            error=_s(obj, "error"),
        )
    if t == RFQ_UNMET_DEMAND_SUMMARY:
        items = []
        for it in obj.get("rfqs") or []:
            b = it.get("requested_size_bucket") or {}
            items.append(UnmetDemandItem(
                reason=_s(it, "reason"), direction=_s(it, "direction").upper(), side=_s(it, "side").upper() or "YES",
                leg_position_ids=_legs(it), condition_id=_s(it, "condition_id"), bucket_unit=_s(b, "unit"),
                min_value_e6=_i(b, "min_value_e6"), max_value_e6=_i(b, "max_value_e6"), count=_i(it, "count"),
            ))
        return UnmetDemandSummary(type=t, window_ms=_i(obj, "window_ms"), total_unfilled=_i(obj, "total_unfilled"),
                                  items=tuple(items))
    return RawMessage(type=t, payload=obj)


def parse_text(text: str) -> Inbound:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"invalid JSON: {e}") from e
    return parse_inbound(obj)


def extract_ids(obj: Any) -> tuple[str, str, str]:
    """Best-effort (type, rfq_id, quote_id) from a decoded frame, for indexing raw rows."""
    if not isinstance(obj, dict):
        return "", "", ""
    return _s(obj, "type"), _s(obj, "rfq_id"), _s(obj, "quote_id")
