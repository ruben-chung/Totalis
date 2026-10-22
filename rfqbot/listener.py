"""Listener: socket -> raw log + raw table -> normalizer, plus the expiry sweeper
and catalog refresh. The paper-quote hook is where the pricing stage plugs in.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Optional

from . import messages as m
from .catalog import CatalogSync
from .config import Settings
from .normalizer import Normalizer, PaperQuote
from .rawlog import RawLog
from .store import Store, now_ms
from .ws_client import AuthResult, Connected, Disconnected, FatalGatewayError, Frame, RfqSocket

log = logging.getLogger(__name__)

QuoteHandler = Callable[[m.RfqRequest, Store], Optional[PaperQuote]]


def observe_only(_rfq: m.RfqRequest, _store: Store) -> Optional[PaperQuote]:
    """Default handler: record everything, quote nothing."""
    return None


class Listener:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        on_rfq: QuoteHandler = observe_only,
        socket: RfqSocket | None = None,
        catalog: CatalogSync | None = None,
        rawlog: RawLog | None = None,
        sweep_interval_s: float = 1.0,
        commit_interval_s: float = 0.25,
    ):
        self.settings = settings
        self.store = store
        self.on_rfq = on_rfq
        self.norm = Normalizer(store, expiry_grace_ms=settings.expiry_grace_ms)
        self.socket = socket or RfqSocket(settings.ws_url, settings.auth_message)
        self.catalog = catalog
        self.rawlog = rawlog
        self.sweep_interval_s = sweep_interval_s
        self.commit_interval_s = commit_interval_s
        self.raw_db_types = settings.raw_db_types
        self.counts: dict[str, int] = {}
        self.fatal: str | None = None
        self._tx_opened = 0.0

    # ------------------------------------------------------------------ run
    async def run(self) -> None:
        if not self.settings.has_auth:
            log.warning("connecting UNAUTHENTICATED (missing %s): RFQ/trade feed is public, but confirmations and "
                        "execution updates for our own quotes will not be delivered", ", ".join(self.settings.missing))
        tasks = [asyncio.create_task(self._socket_loop(), name="socket"),
                 asyncio.create_task(self._sweeper(), name="sweeper")]
        if self.catalog is not None:
            tasks.append(asyncio.create_task(self.catalog.run(), name="catalog"))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                t.result()  # re-raise AuthError / FatalGatewayError
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._commit()
            if self.rawlog is not None:
                self.rawlog.close()

    def stop(self) -> None:
        self.socket.stop()

    # ----------------------------------------------------- batched commits
    # Frames arrive at ~200/s; one BEGIN/COMMIT per frame is wasteful. We keep a
    # transaction open (Store.begin) and commit every commit_interval_s. Store.tx()
    # nests inside it as a savepoint, so a failing handler rolls back only its message.
    def _begin_if_needed(self) -> None:
        if not self.store.in_transaction:
            self.store.begin()
            self._tx_opened = time.monotonic()

    def _commit(self) -> None:
        if self.store.in_transaction:
            self.store.commit()

    def _maybe_commit(self) -> None:
        if self.store.in_transaction and time.monotonic() - self._tx_opened >= self.commit_interval_s:
            self._commit()

    # ---------------------------------------------------------------- loops
    async def _sweeper(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_interval_s)
            self._commit()  # make the batch visible to readers at least once per second
            self._begin_if_needed()
            try:
                expired = self.norm.expire_stale(now_ms())
            except Exception:  # keep sweeping; a failed sweep is retried next tick
                log.exception("expiry sweep failed")
                expired = []
            if expired:
                self._bump("expired", len(expired))
            self._commit()
            if self.rawlog is not None:
                self.rawlog.flush()

    async def _socket_loop(self) -> None:
        async for ev in self.socket.events():
            self._begin_if_needed()
            if isinstance(ev, Connected):
                self.store.connection_opened(ev.conn_id, ev.attempt, ev.at_ms, ev.auth_sent)
                log.info("connected %s (attempt %d, auth %s)", ev.conn_id, ev.attempt,
                         "sent" if ev.auth_sent else "skipped")
            elif isinstance(ev, AuthResult):
                self.store.connection_auth(ev.conn_id, ev.ok, ev.role, ev.address, ev.error)
                if ev.ok:
                    log.info("authenticated role=%s address=%s", ev.role or "?", ev.address or "?")
                else:
                    log.warning("auth rejected: %s (feed continues unauthenticated)", ev.error)
            elif isinstance(ev, Disconnected):
                self.store.connection_closed(ev.conn_id, ev.code, ev.reason, ev.at_ms)
                log.warning("disconnected %s code=%s reason=%s", ev.conn_id, ev.code, ev.reason)
                self._commit()
            elif isinstance(ev, Frame):
                self._on_frame(ev)
                if self.fatal:
                    self._commit()
                    self.socket.stop(fatal=self.fatal)
            self._maybe_commit()
        self._commit()
        if self.fatal:
            raise FatalGatewayError(self.fatal)

    # ---------------------------------------------------------------- frames
    def _on_frame(self, fr: Frame) -> None:
        # 1. raw first, always: the gz log gets every frame; the DB gets the low-volume ones.
        if self.rawlog is not None:
            self.rawlog.write(at_ms=fr.received_at_ms, direction="in", conn_id=fr.conn_id, seq=fr.seq, text=fr.text)
        try:
            obj = json.loads(fr.text)
            msg_type, rfq_id, quote_id = m.extract_ids(obj)
        except json.JSONDecodeError:
            obj, msg_type, rfq_id, quote_id = None, "UNPARSEABLE", "", ""
        msg_type = msg_type or "UNKNOWN"
        raw_id = None
        if self.raw_db_types == "all" or (self.raw_db_types == "non_request" and msg_type != m.RFQ_REQUEST):
            raw_id = self.store.insert_raw(
                received_at_ms=fr.received_at_ms, direction="in", conn_id=fr.conn_id, seq=fr.seq,
                msg_type=msg_type, rfq_id=rfq_id, quote_id=quote_id, payload=fr.text,
            )
        self._bump(msg_type)
        if obj is None:
            log.warning("unparseable frame: %.200s", fr.text)
            return

        # 2. normalize.
        # Broad except: a single malformed frame must not kill the recorder (the raw frame is already on disk).
        try:
            msg = m.parse_inbound(obj)
            res = self.norm.apply(msg, at_ms=fr.received_at_ms, raw_id=raw_id)
        except Exception as e:
            self._bump("normalize_errors")
            log.warning("normalize error %s: %s: %.300s", msg_type, e, fr.text)
            return
        self._log(msg, res)

        # 3. side effects.
        if isinstance(msg, m.RfqRequest) and res.new_rfq:
            if res.unknown_positions and self.catalog is not None:
                self.catalog.request_refresh()
            self._maybe_quote(msg, fr.received_at_ms)
        elif isinstance(msg, m.RfqError) and msg.fatal:
            self.fatal = (f"gateway error {msg.code}: {msg.error or ''}. This wallet is not enabled as an RFQ maker "
                          "(or signer/maker addresses are wrong). Contact support@polymarket.com for RFQ onboarding.")
            log.error(self.fatal)

    def _maybe_quote(self, rfq: m.RfqRequest, at_ms: int) -> None:
        try:
            quote = self.on_rfq(rfq, self.store)
        except Exception:
            log.exception("quote handler failed for %s", rfq.rfq_id)
            return
        if quote is None:
            return
        self.norm.record_paper_quote(rfq.rfq_id, quote, at_ms=at_ms, is_paper=True)   # no live submission path exists
        # Log the frame we *would* have sent, so replay sees the same decision stream.
        payload = json.dumps({
            "type": m.RFQ_QUOTE, "rfq_id": rfq.rfq_id, "price_e6": str(quote.price_e6),
            "size_e6": str(quote.size_e6), "source": quote.source, "paper": True, "reason": quote.reason,
        })
        if self.rawlog is not None:
            self.rawlog.write(at_ms=at_ms, direction="out", conn_id=None, seq=None, text=payload)
        if self.raw_db_types != "none":
            self.store.insert_raw(received_at_ms=at_ms, direction="out", conn_id=None, seq=None,
                                  msg_type=m.RFQ_QUOTE, rfq_id=rfq.rfq_id, quote_id=None, payload=payload)
        self._bump("paper_quotes")
        log.info("PAPER QUOTE %s price=%.4f size=%.2f %s", rfq.rfq_id, quote.price_e6 / 1e6,
                 quote.size_e6 / 1e6, quote.reason)

    # ----------------------------------------------------------------- misc
    def _bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    @staticmethod
    def _log(msg: m.Inbound, res) -> None:
        if isinstance(msg, m.RfqRequest):
            if log.isEnabledFor(logging.DEBUG):
                log.debug("RFQ %s %s %s=%.2f legs=%d%s", msg.rfq_id, msg.direction, msg.requested_size.unit,
                          msg.requested_size.value_e6 / 1e6, len(msg.leg_position_ids), " (dup)" if res.duplicate else "")
        elif isinstance(msg, m.ExecutionUpdate):
            log.info("EXEC %s %s tx=%s", msg.rfq_id, msg.status, msg.tx_hash or "-")
        elif isinstance(msg, m.TradeBroadcast):
            log.info("TRADE %s %s legs=%d price=%.4f size=%.2f%s", msg.rfq_id, msg.direction,
                     len(msg.leg_position_ids), msg.price_e6 / 1e6, msg.size_e6 / 1e6, " (replay)" if res.duplicate else "")
        elif isinstance(msg, m.ConfirmationRequest):
            log.info("LAST LOOK %s quote=%s fill=%.2f @ %.4f", msg.rfq_id, msg.quote_id,
                     msg.fill_size_e6 / 1e6, msg.price_e6 / 1e6)
        elif isinstance(msg, m.UnmetDemandSummary):
            log.info("UNMET DEMAND window=%ds unfilled=%d combos=%d", msg.window_ms // 1000, msg.total_unfilled,
                     len(msg.items))
        elif isinstance(msg, m.RfqError):
            log.warning("ERROR %s rfq=%s code=%s %s", msg.request_type, msg.rfq_id, msg.code, msg.error)
        elif isinstance(msg, m.RawMessage):
            log.info("unknown message type %s", msg.type)
