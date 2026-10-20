import json
from pathlib import Path

import pytest

from rfqbot import messages as m

FIX = Path(__file__).parent / "fixtures" / "messages.jsonl"


def _fixture_payloads():
    out = []
    for line in FIX.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            obj = json.loads(line)
            if obj["direction"] == "in":
                out.append(obj["payload"])
    return out


def test_every_fixture_frame_parses():
    seen = set()
    for p in _fixture_payloads():
        msg = m.parse_inbound(p)
        seen.add(type(msg).__name__)
    assert {"AuthResponse", "RfqRequest", "AckQuote", "AckQuoteCancel", "ConfirmationRequest",
            "AckConfirmationResponse", "ExecutionUpdate", "TradeBroadcast", "RfqError", "RawMessage"} <= seen


def test_rfq_request_fields():
    msg = m.parse_text(json.dumps({
        "type": "RFQ_REQUEST", "rfq_id": "rfq_x", "requestor_public_id": "r", "leg_position_ids": [1, "2"],
        "condition_id": "0xc", "yes_position_id": "y", "no_position_id": "n", "direction": "buy", "side": "yes",
        "requested_size": {"unit": "notional", "value_e6": "1000000"}, "submission_deadline": "1760000001400",
    }))
    assert isinstance(msg, m.RfqRequest)
    assert msg.leg_position_ids == ("1", "2")
    assert msg.direction == "BUY" and msg.side == "YES"
    assert msg.requested_size.value_e6 == 1_000_000
    assert msg.submission_deadline_ms == 1760000001400


def test_execution_terminal_flags():
    assert m.parse_inbound({"type": "RFQ_EXECUTION_UPDATE", "rfq_id": "r", "status": "CONFIRMED"}).terminal
    assert not m.parse_inbound({"type": "RFQ_EXECUTION_UPDATE", "rfq_id": "r", "status": "MINED"}).terminal


def test_error_fatal_flags():
    assert m.parse_inbound({"type": "RFQ_ERROR", "code": "UNAUTHORIZED_ROLE"}).fatal
    assert m.parse_inbound({"type": "RFQ_ERROR", "code": "ADDRESS_MISMATCH"}).fatal
    assert not m.parse_inbound({"type": "RFQ_ERROR", "code": "RATE_LIMITED"}).fatal
    assert not m.parse_inbound({"type": "RFQ_ERROR", "code": "SOMETHING_NEW"}).fatal


def test_unknown_type_kept_raw():
    msg = m.parse_inbound({"type": "NEW_THING", "a": 1})
    assert isinstance(msg, m.RawMessage) and msg.payload == {"type": "NEW_THING", "a": 1}


def test_parse_errors():
    with pytest.raises(m.ParseError):
        m.parse_text("not json")
    with pytest.raises(m.ParseError):
        m.parse_inbound({"no": "type"})
    with pytest.raises(m.ParseError):
        m.parse_inbound(["list"])  # type: ignore[arg-type]
