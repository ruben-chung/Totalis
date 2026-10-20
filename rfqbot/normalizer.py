"""Turns parsed gateway messages into normalized RFQ / quote / execution / trade records.

RFQ status machine
    RECEIVED -> QUOTED -> SELECTED -> EXECUTING -> SETTLED | FAILED
    side exits: EXPIRED (derived by the sweeper), FILLED_BY_OTHER (trade broadcast
    for an RFQ we were not executed on), CANCELLED (we withdrew our quote).

The normalizer never reads the wall clock. Every entry point takes `at_ms`, so a
replay with recorded timestamps produces exactly the same state as live
ingestion and never uses future information.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import messages as m
from .config import ACCEPTANCE_WINDOW_MS, LAST_LOOK_WINDOW_MS, SUBMISSION_WINDOW_MS
from .store import Store

log = logging.getLogger(__name__)

# RFQ statuses
RECEIVED = "RECEIVED"
QUOTED = "QUOTED"
SELECTED = "SELECTED"
EXECUTING = "EXECUTING"
SETTLED = "SETTLED"
FAILED = "FAILED"
EXPIRED = "EXPIRED"
FILLED_BY_OTHER = "FILLED_BY_OTHER"
CANCELLED = "CANCELLED"
OPEN_STATUSES = (RECEIVED, QUOTED)
TERMINAL_STATUSES = {SETTLED, FAILED, EXPIRED, FILLED_BY_OTHER, CANCELLED}

# Quote states
Q_SUBMITTED = "SUBMITTED"
Q_ACKED = "ACKED"
Q_SUPERSEDED = "SUPERSEDED"
Q_CANCELLED = "CANCELLED"
Q_REJECTED = "REJECTED"
Q_SELECTED = "SELECTED"
Q_CONFIRMED = "CONFIRMED"
Q_DECLINED = "DECLINED"
Q_FILLED = "FILLED"
Q_EXPIRED = "EXPIRED"
Q_LIVE = {Q_SUBMITTED, Q_ACKED, Q_SELECTED, Q_CONFIRMED}

# Event types (rfq_events.event_type). RECEIVED/EXPIRED/FILLED_BY_OTHER are implied by rfqs.status.
EV_RECEIVED_DUP = "RECEIVED_DUPLICATE"
EV_STUB = "STUB_CREATED"
EV_QUOTE_DRAFTED = "QUOTE_DRAFTED"
EV_QUOTE_REVISED = "QUOTE_REVISED"
EV_QUOTE_ACKED = "QUOTE_ACKED"
EV_QUOTE_CANCELLED = "QUOTE_CANCELLED"
EV_QUOTE_REJECTED = "QUOTE_REJECTED"
EV_CONFIRMATION_REQUESTED = "CONFIRMATION_REQUESTED"
EV_CONFIRMATION_SENT = "CONFIRMATION_SENT"
EV_EXECUTION_UPDATE = "EXECUTION_UPDATE"
EV_TRADE_BROADCAST = "TRADE_BROADCAST"
EV_SETTLED = "SETTLED"
EV_ERROR = "ERROR"


@dataclass
class PaperQuote:
    price_e6: int
    size_e6: int
    source: str = "collateral"      # "collateral" | "inventory"
    reason: str = ""                 # short explanation of pricing adjustments


@dataclass
class ApplyResult:
    msg: m.Inbound
    rfq_id: str = ""
    new_rfq: bool = False
    status: str | None = None
    unknown_positions: list[str] = field(default_factory=list)
    duplicate: bool = False


class Normalizer:
    def __init__(self, store: Store, *, expiry_grace_ms: int = 5_000):
        self.store = store
        self.expiry_grace_ms = expiry_grace_ms
        self.horizon_ms = ACCEPTANCE_WINDOW_MS + LAST_LOOK_WINDOW_MS + expiry_grace_ms

    # ------------------------------------------------------------------ apply
    def apply(self, msg: m.Inbound, *, at_ms: int, raw_id: int | None = None) -> ApplyResult:
        with self.store.tx():
            if isinstance(msg, m.RfqRequest):
                return self._on_request(msg, at_ms, raw_id)
            if isinstance(msg, m.TradeBroadcast):
                return self._on_trade(msg, at_ms, raw_id)
            if isinstance(msg, m.AckQuote):
                return self._on_ack_quote(msg, at_ms, raw_id)
            if isinstance(msg, m.AckQuoteCancel):
                return self._on_ack_cancel(msg, at_ms, raw_id)
            if isinstance(msg, m.ConfirmationRequest):
                return self._on_confirmation_request(msg, at_ms, raw_id)
            if isinstance(msg, m.AckConfirmationResponse):
                return self._on_ack_confirmation(msg, at_ms, raw_id)
            if isinstance(msg, m.ExecutionUpdate):
                return self._on_execution(msg, at_ms, raw_id)
            if isinstance(msg, m.RfqError):
                return self._on_error(msg, at_ms, raw_id)
            if isinstance(msg, m.UnmetDemandSummary):
                return self._on_unmet_demand(msg, at_ms, raw_id)
            return ApplyResult(msg=msg)  # AuthResponse / RawMessage: nothing to normalize

    # ----------------------------------------------------------- paper quotes
    def record_paper_quote(self, rfq_id: str, quote: PaperQuote, *, at_ms: int, is_paper: bool = True) -> int:
        """Store a new quote revision for an RFQ. Returns the quote local_id."""
        s = self.store
        with s.tx():
            rfq = s.get_rfq(rfq_id)
            if rfq is None:
                raise KeyError(f"unknown rfq {rfq_id}")
            prev = s.latest_quote(rfq_id)
            if prev is not None and prev["state"] in Q_LIVE:
                s.update_quote_state(prev["local_id"], Q_SUPERSEDED, at_ms)
            rev = s.next_quote_revision(rfq_id)
            local_id = s.insert_quote(
                rfq_id=rfq_id, revision=rev, price_e6=quote.price_e6, size_e6=quote.size_e6,
                source=quote.source, is_paper=is_paper, state=Q_SUBMITTED, at_ms=at_ms, reason=quote.reason or None,
            )
            s.add_event(rfq_id, EV_QUOTE_DRAFTED if rev == 1 else EV_QUOTE_REVISED, at_ms, details={
                "revision": rev, "price_e6": quote.price_e6, "size_e6": quote.size_e6,
                "source": quote.source, "reason": quote.reason, "paper": is_paper,
            })
            if rfq["status"] == RECEIVED:
                s.set_rfq_status(rfq_id, QUOTED, at_ms)
            return local_id

    # --------------------------------------------------------------- sweeper
    def expire_stale(self, now_ms: int) -> list[str]:
        """Derive expiry: no explicit expire message reaches makers.

        An RFQ still open after submission + acceptance + last-look windows plus a
        grace period, with no execution update, is marked EXPIRED.
        """
        s = self.store
        with s.tx():
            ids = s.expire_open_rfqs(cutoff_ms=now_ms, horizon_ms=self.horizon_ms, now_ms=now_ms,
                                     default_window_ms=SUBMISSION_WINDOW_MS)
            if ids:
                s.expire_live_quotes(ids, now_ms, Q_EXPIRED)
        return ids

    # ------------------------------------------------------------- handlers
    def _on_request(self, msg: m.RfqRequest, at_ms: int, raw_id: int | None) -> ApplyResult:
        s = self.store
        combo_id = s.combo_id(msg.condition_id, msg.leg_position_ids, yes=msg.yes_position_id,
                              no=msg.no_position_id, at_ms=at_ms)
        new = s.insert_rfq({
            "rfq_id": msg.rfq_id,
            "requester_id": s.requester_id(msg.requestor_public_id),
            "combo_id": combo_id,
            "direction": msg.direction,
            "side": msg.side,
            "size_unit": msg.requested_size.unit,
            "size_value_e6": msg.requested_size.value_e6,
            "submission_deadline_ms": msg.submission_deadline_ms or None,
            "first_seen_ms": at_ms,
            "status": RECEIVED,
            "status_at_ms": at_ms,
            "stub": 0,
        })
        if not new:
            existing = s.get_rfq(msg.rfq_id)
            if existing["stub"]:
                # A later-lifecycle message created a placeholder; fill in the request details now.
                s.conn.execute(
                    "UPDATE rfqs SET requester_id=?, combo_id=?, direction=?, side=?, size_unit=?, size_value_e6=?,"
                    " submission_deadline_ms=?, stub=0 WHERE rfq_id=?",
                    (s.requester_id(msg.requestor_public_id), combo_id, msg.direction, msg.side,
                     msg.requested_size.unit, msg.requested_size.value_e6, msg.submission_deadline_ms or None,
                     msg.rfq_id),
                )
                return ApplyResult(msg=msg, rfq_id=msg.rfq_id, status=existing["status"])
            s.add_event(msg.rfq_id, EV_RECEIVED_DUP, at_ms, raw_id)
            return ApplyResult(msg=msg, rfq_id=msg.rfq_id, duplicate=True, status=existing["status"])
        unknown = s.unknown_positions(msg.leg_position_ids)
        return ApplyResult(msg=msg, rfq_id=msg.rfq_id, new_rfq=True, status=RECEIVED, unknown_positions=unknown)

    def _on_ack_quote(self, msg: m.AckQuote, at_ms: int, raw_id: int | None) -> ApplyResult:
        s = self.store
        q = s.latest_quote(msg.rfq_id)
        if q is not None:
            s.update_quote_state(q["local_id"], Q_ACKED, at_ms, server_quote_id=msg.quote_id)
        s.add_event(msg.rfq_id, EV_QUOTE_ACKED, at_ms, raw_id, {"quote_id": msg.quote_id})
        rfq = s.get_rfq(msg.rfq_id)
        status = rfq["status"] if rfq else None
        if rfq is not None and rfq["status"] == RECEIVED:
            s.set_rfq_status(msg.rfq_id, QUOTED, at_ms)
            status = QUOTED
        return ApplyResult(msg=msg, rfq_id=msg.rfq_id, status=status)

    def _on_ack_cancel(self, msg: m.AckQuoteCancel, at_ms: int, raw_id: int | None) -> ApplyResult:
        s = self.store
        q = s.quote_by_server_id(msg.quote_id) or s.latest_quote(msg.rfq_id)
        if q is not None:
            s.update_quote_state(q["local_id"], Q_CANCELLED, at_ms)
        s.add_event(msg.rfq_id, EV_QUOTE_CANCELLED, at_ms, raw_id, {"quote_id": msg.quote_id})
        rfq = s.get_rfq(msg.rfq_id)
        if rfq is not None and rfq["status"] in OPEN_STATUSES:
            s.set_rfq_status(msg.rfq_id, CANCELLED, at_ms)
        return ApplyResult(msg=msg, rfq_id=msg.rfq_id, status=CANCELLED)

    def _on_confirmation_request(self, msg: m.ConfirmationRequest, at_ms: int, raw_id: int | None) -> ApplyResult:
        s = self.store
        new = self._ensure_rfq_stub(msg.rfq_id, at_ms, status=SELECTED, condition_id=msg.condition_id,
                                    yes=msg.yes_position_id, no=msg.no_position_id, direction=msg.direction,
                                    side=msg.side, legs=msg.leg_position_ids)
        s.set_rfq_status(msg.rfq_id, SELECTED, at_ms)
        q = s.quote_by_server_id(msg.quote_id) or s.latest_quote(msg.rfq_id)
        if q is not None:
            s.update_quote_state(q["local_id"], Q_SELECTED, at_ms, server_quote_id=msg.quote_id)
        s.add_event(msg.rfq_id, EV_CONFIRMATION_REQUESTED, at_ms, raw_id, {
            "quote_id": msg.quote_id, "fill_size_e6": msg.fill_size_e6, "price_e6": msg.price_e6,
            "confirm_by_ms": msg.confirm_by_ms,
        })
        return ApplyResult(msg=msg, rfq_id=msg.rfq_id, new_rfq=new, status=SELECTED)

    def _on_ack_confirmation(self, msg: m.AckConfirmationResponse, at_ms: int, raw_id: int | None) -> ApplyResult:
        s = self.store
        q = s.quote_by_server_id(msg.quote_id) or s.latest_quote(msg.rfq_id)
        state = Q_CONFIRMED if msg.decision.upper() == "CONFIRM" else Q_DECLINED
        if q is not None:
            s.update_quote_state(q["local_id"], state, at_ms)
        s.add_event(msg.rfq_id, EV_CONFIRMATION_SENT, at_ms, raw_id, {"quote_id": msg.quote_id, "decision": msg.decision})
        return ApplyResult(msg=msg, rfq_id=msg.rfq_id)

    def _on_execution(self, msg: m.ExecutionUpdate, at_ms: int, raw_id: int | None) -> ApplyResult:
        s = self.store
        new = self._ensure_rfq_stub(msg.rfq_id, at_ms, status=EXECUTING)
        s.insert_execution(msg.rfq_id, msg.quote_id, msg.status, msg.tx_hash, at_ms)
        s.add_event(msg.rfq_id, EV_EXECUTION_UPDATE, at_ms, raw_id, {"status": msg.status, "tx_hash": msg.tx_hash})
        q = s.latest_quote(msg.rfq_id)
        if msg.status == "CONFIRMED":
            status = SETTLED
            s.insert_settlement(msg.rfq_id, msg.tx_hash, at_ms)
            s.add_event(msg.rfq_id, EV_SETTLED, at_ms, raw_id, {"tx_hash": msg.tx_hash})
            s.mark_trade_ours(msg.rfq_id)
            if q is not None:
                s.update_quote_state(q["local_id"], Q_FILLED, at_ms)
        elif msg.status == "FAILED":
            status = FAILED
        else:
            status = EXECUTING
        s.set_rfq_status(msg.rfq_id, status, at_ms)
        return ApplyResult(msg=msg, rfq_id=msg.rfq_id, new_rfq=new, status=status)

    def _on_trade(self, msg: m.TradeBroadcast, at_ms: int, raw_id: int | None) -> ApplyResult:
        s = self.store
        rfq = s.get_rfq(msg.rfq_id)
        ours = s.has_execution(msg.rfq_id) or (rfq is not None and rfq["status"] in (SELECTED, EXECUTING, SETTLED))
        combo_id = s.combo_id(msg.condition_id, msg.leg_position_ids, at_ms=at_ms)
        inserted = s.insert_trade({
            "rfq_id": msg.rfq_id, "requester_id": s.requester_id(msg.requester_id), "combo_id": combo_id,
            "direction": msg.direction, "side": msg.side, "price_e6": msg.price_e6, "size_e6": msg.size_e6,
            "executed_at_ms": msg.executed_at_ms or at_ms, "received_at_ms": at_ms,
            "is_ours": 1 if ours else 0, "raw_message_id": raw_id,
        })
        if not inserted:
            # Replayed broadcast after a reconnect: already recorded.
            return ApplyResult(msg=msg, rfq_id=msg.rfq_id, duplicate=True, status=rfq["status"] if rfq else None)
        new = self._ensure_rfq_stub(msg.rfq_id, at_ms, status=FILLED_BY_OTHER, condition_id=msg.condition_id,
                                    direction=msg.direction, side=msg.side, legs=msg.leg_position_ids,
                                    requestor=msg.requester_id)
        s.add_event(msg.rfq_id, EV_TRADE_BROADCAST, at_ms, raw_id, {
            "price_e6": msg.price_e6, "size_e6": msg.size_e6, "executed_at_ms": msg.executed_at_ms, "ours": ours,
        })
        status = None
        if not ours:
            rfq = s.get_rfq(msg.rfq_id)
            # EXPIRED included: trade broadcasts arrive 7-17s after the request, past the derived expiry horizon.
            if new or rfq["status"] in OPEN_STATUSES or rfq["status"] in (CANCELLED, EXPIRED):
                s.set_rfq_status(msg.rfq_id, FILLED_BY_OTHER, at_ms)
                s.expire_live_quotes([msg.rfq_id], at_ms, Q_EXPIRED, reason="filled by another maker")
                status = FILLED_BY_OTHER
        return ApplyResult(msg=msg, rfq_id=msg.rfq_id, new_rfq=new, status=status)

    def _on_error(self, msg: m.RfqError, at_ms: int, raw_id: int | None) -> ApplyResult:
        s = self.store
        if not msg.rfq_id:
            return ApplyResult(msg=msg)
        if msg.request_type == m.RFQ_QUOTE:
            q = s.quote_by_server_id(msg.quote_id) or s.latest_quote(msg.rfq_id)
            if q is not None:
                s.update_quote_state(q["local_id"], Q_REJECTED, at_ms, reason=msg.code)
            s.add_event(msg.rfq_id, EV_QUOTE_REJECTED, at_ms, raw_id, {"code": msg.code, "error": msg.error})
        else:
            s.add_event(msg.rfq_id, EV_ERROR, at_ms, raw_id,
                        {"request_type": msg.request_type, "code": msg.code, "error": msg.error})
        return ApplyResult(msg=msg, rfq_id=msg.rfq_id)

    def _on_unmet_demand(self, msg: m.UnmetDemandSummary, at_ms: int, raw_id: int | None) -> ApplyResult:
        s = self.store
        items = []
        for it in msg.items:
            items.append({
                "combo_id": s.combo_id(it.condition_id, it.leg_position_ids, at_ms=at_ms),
                "reason": it.reason, "direction": it.direction, "side": it.side, "bucket_unit": it.bucket_unit,
                "min_value_e6": it.min_value_e6, "max_value_e6": it.max_value_e6, "count": it.count,
            })
        s.insert_unmet_demand(at_ms=at_ms, window_ms=msg.window_ms, total_unfilled=msg.total_unfilled,
                              raw_message_id=raw_id, items=items)
        return ApplyResult(msg=msg)

    # ------------------------------------------------------------------ util
    def _ensure_rfq_stub(self, rfq_id: str, at_ms: int, *, status: str, condition_id: str = "", yes: str = "",
                         no: str = "", direction: str = "", side: str = "", legs: tuple[str, ...] = (),
                         requestor: str = "") -> bool:
        """Create a placeholder RFQ row when a later-lifecycle message arrives first
        (e.g. the RFQ_REQUEST was missed during a reconnect gap). Returns True if created."""
        s = self.store
        if s.get_rfq(rfq_id) is not None:
            return False
        s.insert_rfq({
            "rfq_id": rfq_id, "requester_id": s.requester_id(requestor),
            "combo_id": s.combo_id(condition_id, legs, yes=yes, no=no, at_ms=at_ms),
            "direction": direction or None, "side": side or None, "size_unit": None, "size_value_e6": None,
            "submission_deadline_ms": None, "first_seen_ms": at_ms, "status": status, "status_at_ms": at_ms,
            "stub": 1,
        })
        s.add_event(rfq_id, EV_STUB, at_ms, details={"reason": "request not seen"})
        return True
