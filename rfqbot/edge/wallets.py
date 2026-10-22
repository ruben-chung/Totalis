"""Wallet scores and the sequential block rule.

Per requester public_id: running mean and variance of our realised profit per fill (markup earned
plus the residual 30-minute markout, i.e. the print's markout minus its node's mean markout, which
the node already charges as ``info_premium``; probability points of the combo), shrunk toward the
population mean with ``shrink_n`` pseudo-fills. The rule, evaluated at every refresh (so it is
sequential): block when the wallet has at least ``block_min_fills`` fills and the one-sided
``block_conf`` upper bound of its expected profit per fill is below zero. A block lasts
``block_days`` unless re-triggered. An actual adverse round trip (we filled the wallet and it printed
the opposite side of the same combo within 10 s, ``rfqbot.edge.fills.adverse_round_trips``) blocks
immediately; quote assembly's ``[round_trip]`` tag (the wallet merely polled both sides, ~43% of
quotes) is a descriptor and never blocks. Manual list: ``allow`` never blocks and pays no premium,
``deny`` always blocks. Below the threshold the shrunk score is a per-wallet premium
``wallet_k * max(0, -expected profit)``.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .config import EdgeConfig

log = logging.getLogger(__name__)

Z_CONF = {0.80: 0.8416, 0.90: 1.2816, 0.95: 1.6449, 0.975: 1.96, 0.99: 2.3263}


def z_for(conf: float) -> float:
    if conf in Z_CONF:
        return Z_CONF[conf]
    # Newton on Phi(z) = conf
    z = 1.0
    for _ in range(50):
        p = 0.5 * math.erfc(-z / math.sqrt(2)) - conf
        d = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
        z -= p / d
    return z


@dataclass(frozen=True)
class WalletObs:
    public_id: str
    profit: float          # markup earned + residual 30-min markout (maker terms, probability points)
    at_ms: int
    win: bool = True       # counterfactual win at our actual quote (False: scored on the print as if filled)
    round_trip: bool = False   # an actual adverse round trip on this fill (not the quoting descriptor tag)


@dataclass
class WalletScore:
    public_id: str
    n: int
    n_wins: int
    mean: float
    sd: float
    shrunk: float          # posterior-style mean toward the population
    se: float
    upper: float           # one-sided upper confidence bound of expected profit per fill
    lower: float
    blocked: bool
    blocked_until_ms: int | None
    reason: str            # rule | round_trip | manual | carried | ""
    premium: float
    manual: str = ""       # allow | deny | ""
    last_ms: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


_last_good_manual: dict[str, dict[str, str]] = {}   # path -> the last list that parsed


def load_manual(path: str | Path) -> dict[str, str]:
    """The manual allow / deny list. A malformed file is logged at WARNING and the last list that parsed
    from that path is kept (a half-written edit must not silently drop a deny)."""
    p = Path(path)
    key = str(p)
    if not p.exists():
        return dict(_last_good_manual.get(key, {}))
    try:
        raw = json.loads(p.read_text() or "{}")
        out: dict[str, str] = {}
        for k in ("allow", "deny"):
            for pid in raw.get(k, []) or []:
                out[str(pid)] = k
    except Exception as e:
        log.warning("wallet list %s is malformed (%s: %s); keeping the last good list (%d entries)", p, type(e).__name__, e,
                    len(_last_good_manual.get(key, {})))
        return dict(_last_good_manual.get(key, {}))
    _last_good_manual[key] = dict(out)
    return out


def save_manual(path: str | Path, manual: Mapping[str, str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"allow": sorted(k for k, v in manual.items() if v == "allow"),
            "deny": sorted(k for k, v in manual.items() if v == "deny")}
    p.write_text(json.dumps(data, indent=1) + "\n")


def score_wallets(obs: Iterable[WalletObs], cfg: EdgeConfig, now_ms: int, *,
                  prior: Mapping[str, WalletScore] | None = None,
                  manual: Mapping[str, str] | None = None) -> dict[str, WalletScore]:
    prior = prior or {}
    manual = manual or {}
    by: dict[str, list[WalletObs]] = {}
    for o in obs:
        if o.public_id:
            by.setdefault(o.public_id, []).append(o)
    allp = [o.profit for v in by.values() for o in v]
    pop_mean = sum(allp) / len(allp) if allp else 0.0
    pop_var = (sum((x - pop_mean) ** 2 for x in allp) / (len(allp) - 1)) if len(allp) > 1 else 0.0
    z = z_for(cfg.block_conf)
    k = cfg.shrink_n
    out: dict[str, WalletScore] = {}
    ids = set(by) | set(prior) | set(manual)
    for pid in sorted(ids):
        xs = by.get(pid, [])
        n = len(xs)
        mean = sum(o.profit for o in xs) / n if n else pop_mean
        var_w = (sum((o.profit - mean) ** 2 for o in xs) / (n - 1)) if n > 1 else pop_var
        # pooled variance: the wallet's own once it has 30 fills, the population's before
        var = var_w if n >= 30 else pop_var
        shrunk = (n * mean + k * pop_mean) / (n + k) if (n + k) > 0 else pop_mean
        se = math.sqrt(var / (n + k)) if (n + k) > 0 and var > 0 else 0.0
        upper, lower = shrunk + z * se, shrunk - z * se
        rt = any(o.round_trip for o in xs)
        man = manual.get(pid, "")
        old = prior.get(pid)
        blocked, until, reason = False, None, ""
        if man == "deny":
            blocked, reason = True, "manual"
        elif man == "allow":
            blocked, reason = False, ""
        elif rt:
            blocked, until, reason = True, now_ms + int(cfg.block_days * 86_400_000), "round_trip"
        elif n >= cfg.block_min_fills and upper < 0:
            blocked, until, reason = True, now_ms + int(cfg.block_days * 86_400_000), "rule"
        elif old is not None and old.blocked and old.blocked_until_ms is not None and old.blocked_until_ms > now_ms:
            blocked, until, reason = True, old.blocked_until_ms, "carried"
        premium = 0.0 if (blocked or man == "allow") else cfg.wallet_k * max(0.0, -shrunk)
        out[pid] = WalletScore(public_id=pid, n=n, n_wins=sum(1 for o in xs if o.win), mean=mean, sd=math.sqrt(var_w) if var_w > 0 else 0.0,
                               shrunk=shrunk, se=se, upper=upper, lower=lower, blocked=blocked, blocked_until_ms=until,
                               reason=reason, premium=premium, manual=man, last_ms=max((o.at_ms for o in xs), default=(old.last_ms if old else 0)))
    return out
