"""Websocket client for the Combos RFQ quoter gateway.

Responsibilities: connect, (optionally) authenticate, keep alive, reconnect with
backoff. It knows nothing about storage. It yields a stream of events:

    Connected(conn_id, attempt, at_ms, auth_sent)
    AuthResult(conn_id, ok, role, address, error, at_ms)   # whenever an auth frame arrives
    Frame(conn_id, seq, received_at_ms, text)              # every inbound text frame, verbatim
    Disconnected(conn_id, code, reason, at_ms)

Observed behaviour (2026-10-20): the gateway streams RFQ_REQUEST / RFQ_TRADE /
RFQ_UNMET_DEMAND_SUMMARY to any connection, authenticated or not; the auth reply
arrives asynchronously among the stream. Auth is only needed to receive
confirmation / execution frames for quotes we actually submit, so an auth
failure is reported but does not stop the listener unless `require_auth`.

Keepalive: the gateway pings every 30 s with payload b"rfq"; `websockets`
replies with a matching pong automatically. We add an idle watchdog on top.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator

import websockets
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from . import messages as m

log = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """Credentials were rejected repeatedly and require_auth is set."""


class FatalGatewayError(RuntimeError):
    """The gateway sent an error that means our identity cannot quote (e.g. UNAUTHORIZED_ROLE)."""


@dataclass(frozen=True)
class Connected:
    conn_id: str
    attempt: int
    at_ms: int
    auth_sent: bool


@dataclass(frozen=True)
class AuthResult:
    conn_id: str
    ok: bool
    role: str
    address: str
    error: str
    at_ms: int


@dataclass(frozen=True)
class Frame:
    conn_id: str
    seq: int
    received_at_ms: int
    text: str


@dataclass(frozen=True)
class Disconnected:
    conn_id: str
    code: int | None
    reason: str
    at_ms: int


Event = Connected | AuthResult | Frame | Disconnected


def _now_ms() -> int:
    return int(time.time() * 1000)


class RfqSocket:
    def __init__(
        self,
        url: str,
        auth_message: dict | None,
        *,
        require_auth: bool = False,
        idle_timeout_s: float = 90.0,
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 60.0,
        max_auth_failures: int = 3,
        ping_interval_s: float | None = 20.0,
    ):
        self.url = url
        self.auth_message = auth_message
        self.require_auth = require_auth
        self.idle_timeout_s = idle_timeout_s
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.max_auth_failures = max_auth_failures
        self.ping_interval_s = ping_interval_s
        self._stop = asyncio.Event()
        self._ws: ClientConnection | None = None
        self._fatal: str | None = None
        self.attempt = 0
        self.consecutive_auth_failures = 0

    # ---------------------------------------------------------------- control
    def stop(self, *, fatal: str | None = None) -> None:
        if fatal:
            self._fatal = fatal
        self._stop.set()
        ws = self._ws
        if ws is not None:
            asyncio.ensure_future(ws.close())

    # ---------------------------------------------------------------- events
    async def events(self) -> AsyncIterator[Event]:
        backoff_n = 0
        while not self._stop.is_set():
            self.attempt += 1
            conn_id = f"{_now_ms():x}-{uuid.uuid4().hex[:8]}"
            got_frames = False
            try:
                async with connect(self.url, ping_interval=self.ping_interval_s, ping_timeout=self.ping_interval_s,
                                   close_timeout=5, max_size=8 * 1024 * 1024) as ws:
                    self._ws = ws
                    yield Connected(conn_id, self.attempt, _now_ms(), self.auth_message is not None)
                    async for ev in self._session(ws, conn_id):
                        if isinstance(ev, Frame):
                            got_frames = True
                            backoff_n = 0
                        if isinstance(ev, AuthResult):
                            if ev.ok:
                                self.consecutive_auth_failures = 0
                            else:
                                self.consecutive_auth_failures += 1
                                if self.require_auth and self.consecutive_auth_failures >= self.max_auth_failures:
                                    yield ev
                                    raise AuthError(
                                        f"authentication rejected {self.consecutive_auth_failures} times: {ev.error}. "
                                        "Check POLYMARKET_API_KEY / SECRET / PASSPHRASE and signer/maker addresses."
                                    )
                        yield ev
            except AuthError:
                raise
            except ConnectionClosed as e:
                rc = e.rcvd or e.sent
                code = rc.code if rc else None
                reason = (rc.reason if rc else "") or "connection closed"
                yield Disconnected(conn_id, code, reason, _now_ms())
            except (OSError, websockets.InvalidURI, websockets.InvalidHandshake, asyncio.TimeoutError) as e:
                yield Disconnected(conn_id, None, f"{type(e).__name__}: {e}", _now_ms())
            except _IdleTimeout:
                yield Disconnected(conn_id, None, f"idle for {self.idle_timeout_s}s", _now_ms())
            finally:
                self._ws = None

            if self._stop.is_set() or self._fatal:
                break
            if not got_frames:
                backoff_n = min(backoff_n + 1, 10)
            delay = max(0.25, random.uniform(0, min(self.backoff_cap_s, self.backoff_base_s * (2 ** backoff_n))))
            log.warning("reconnecting in %.1fs (attempt %d)", delay, self.attempt + 1)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

        if self._fatal:
            raise FatalGatewayError(self._fatal)

    async def _session(self, ws: ClientConnection, conn_id: str) -> AsyncIterator[Event]:
        if self.auth_message is not None:
            await ws.send(json.dumps(self.auth_message))
        seq = 0
        while not self._stop.is_set():
            try:
                text = await asyncio.wait_for(ws.recv(), timeout=self.idle_timeout_s)
            except asyncio.TimeoutError:
                raise _IdleTimeout()
            if isinstance(text, bytes):
                text = text.decode("utf-8", "replace")
            seq += 1
            now = _now_ms()
            yield Frame(conn_id, seq, now, text)
            # Cheap check before parsing: auth frames are rare and tiny.
            if '"auth"' in text[:40]:
                try:
                    resp = m.parse_text(text)
                except m.ParseError:
                    continue
                if isinstance(resp, m.AuthResponse):
                    yield AuthResult(conn_id, resp.success, resp.role, resp.address, resp.error, now)


class _IdleTimeout(Exception):
    pass
