"""Sport-agnostic contract identity.

A LegSpec describes one tradeable outcome of one market on one venue in structural terms
(game, family, period, subject, line, side) plus the settlement rules that matter when the
same contract is compared across venues. Nothing here knows about NFL specifics; the sport
parsers (nfl_poly, nfl_kalshi) produce LegSpecs and the matcher compares them.

Game identity
-------------
``game_key`` is ``{sport}|{game_date}|{away}@{home}``. ``game_date`` is the local (US/Eastern)
calendar date of kickoff when the parser can know it (Kalshi tickers carry the ET date;
Polymarket when a Gamma ``gameStartTime`` is supplied). When only a Polymarket slug is
available the slug carries the **UTC** date of kickoff, so ``game_date_basis`` is ``"utc_slug"``
and the true ET date is either that date or the day before (US kickoffs never start before
09:00 ET, so UTC date >= ET date, and never after 23:59 ET, so UTC date <= ET date + 1).
The matcher resolves the ambiguity against the Kalshi game index with that one-day
tolerance and asserts uniqueness (no NFL team plays twice within two days).

Line convention
---------------
* spread: ``team`` is the handicapped team, ``line`` is its handicap, always negative
  (``line=-7.5`` means "team wins by 8 or more"). ``side`` is the team code that wins the
  contract: ``side == team`` is the cover, ``side == other team`` is the +7.5 side.
* total / team_total / team_tds / team_off_yards / two_pt_conversions: ``line`` positive,
  ``side`` is ``over`` or ``under`` (``over`` wins iff value > line).
* moneyline: ``line`` is None, ``side`` is the winning team code (or ``tie`` where a venue
  lists it as its own outcome).
* yes/no families (btts, safety, exact_margin, highest_scoring_quarter): ``side`` is
  ``yes``/``no``; ``bucket`` carries the non-team subject (margin band, quarter).

``canonical_key`` is venue-independent, so two LegSpecs with the same key describe the same
contract *modulo settlement rules*; rules live in ``rules`` and are compared separately.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

PERIODS = ("FG", "1H", "2H", "1Q", "2Q", "3Q", "4Q")

# Family registry. kind: "team" (side is a team code), "over_under", "yes_no", or an explicit
# tuple of exactly two side labels (e.g. ("odd", "even")). Sports register their own families
# at import time with ``register_family``; the built-ins below are the NFL set.
FAMILY_KINDS: dict[str, str | tuple[str, str]] = {
    "moneyline": "team", "spread": "team",
    "total": "over_under", "team_total": "over_under", "team_tds": "over_under",
    "team_off_yards": "over_under", "two_pt_conversions": "over_under",
    "btts": "yes_no", "safety": "yes_no", "exact_margin": "yes_no", "highest_scoring_quarter": "yes_no",
}


def register_family(name: str, kind: str | tuple[str, str]) -> None:
    """Add a family. ``kind`` is "team" | "over_under" | "yes_no" | (side_a, side_b). Re-registering
    with the same kind is a no-op; a different kind raises."""
    if isinstance(kind, tuple):
        if len(kind) != 2 or kind[0] == kind[1]:
            raise ValueError(f"explicit side tuple must hold two distinct labels, got {kind!r}")
    elif kind not in ("team", "over_under", "yes_no"):
        raise ValueError(f"unknown family kind {kind!r}")
    if name in FAMILY_KINDS and FAMILY_KINDS[name] != kind:
        raise ValueError(f"family {name!r} already registered as {FAMILY_KINDS[name]!r}")
    FAMILY_KINDS[name] = kind


def family_kind(name: str) -> str | tuple[str, str]:
    try:
        return FAMILY_KINDS[name]
    except KeyError:
        raise LegSpecError(f"bad family {name!r}") from None


def _families_of(kind: str) -> frozenset[str]:
    return frozenset(n for n, k in FAMILY_KINDS.items() if k == kind)


class _KindView(frozenset):  # type: ignore[type-arg]
    """Live view: ``"x" in OVER_UNDER_FAMILIES`` reflects families registered later."""
    kind: str

    def __new__(cls, kind: str):
        o = super().__new__(cls)
        o.kind = kind
        return o

    def __contains__(self, item) -> bool:  # type: ignore[override]
        return FAMILY_KINDS.get(item) == self.kind

    def __iter__(self):
        return iter(_families_of(self.kind))

    def __len__(self) -> int:
        return len(_families_of(self.kind))


TEAM_SIDED_FAMILIES = _KindView("team")
OVER_UNDER_FAMILIES = _KindView("over_under")
YES_NO_FAMILIES = _KindView("yes_no")


class _AllView(frozenset):  # type: ignore[type-arg]
    def __contains__(self, item) -> bool:  # type: ignore[override]
        return item in FAMILY_KINDS

    def __iter__(self):
        return iter(FAMILY_KINDS)

    def __len__(self) -> int:
        return len(FAMILY_KINDS)


FAMILIES = _AllView()


class LegSpecError(ValueError):
    """Raised for an inconsistent LegSpec or an undefined operation on one."""


@dataclass(frozen=True)
class LegSpec:
    venue: str                       # "polymarket" | "kalshi"
    sport: str                       # "nfl"
    away: str                        # canonical team code
    home: str
    game_date: str                   # ISO date, see module docstring
    family: str
    period: str                      # FG | 1H | 2H | 1Q | 2Q | 3Q | 4Q
    side: str                        # team code | over | under | yes | no | tie | not_{team}
    team: str | None = None          # subject team for spread / team_* families, exact margin
    line: float | None = None
    bucket: str | None = None        # non-team subject: margin band "1-6", quarter "Q1", "tie"
    includes_ot: bool | None = None  # None = the venue's rules do not say
    game_date_basis: str = "et"      # "et" | "utc_slug"
    kickoff_utc: datetime | None = field(default=None, compare=False)
    venue_ids: dict[str, str] = field(default_factory=dict, compare=False, hash=False)
    rules: dict[str, str] = field(default_factory=dict, compare=False, hash=False)
    rules_text: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.period not in PERIODS:
            raise LegSpecError(f"bad period {self.period!r}")
        kind = family_kind(self.family)
        if self.away == self.home:
            raise LegSpecError("away == home")
        if self.game_date_basis not in ("et", "utc_slug"):
            raise LegSpecError(f"bad game_date_basis {self.game_date_basis!r}")
        if kind == "over_under" and self.side not in ("over", "under"):
            raise LegSpecError(f"{self.family} side must be over/under, got {self.side!r}")
        if kind == "yes_no" and self.side not in ("yes", "no"):
            raise LegSpecError(f"{self.family} side must be yes/no, got {self.side!r}")
        if isinstance(kind, tuple) and self.side not in kind:
            raise LegSpecError(f"{self.family} side must be one of {kind}, got {self.side!r}")
        if kind == "team" and self.family not in ("moneyline", "spread"):
            if self.side not in (self.away, self.home) and not self.side.startswith("not_"):
                raise LegSpecError(f"{self.family} side must be a team code, got {self.side!r}")
        if self.family == "spread":
            if self.team not in (self.away, self.home) or self.line is None or self.line >= 0:
                raise LegSpecError("spread needs team in game and negative line")
            if self.side not in (self.away, self.home):
                raise LegSpecError(f"spread side must be a team code, got {self.side!r}")
        if self.family == "moneyline":
            if self.side not in (self.away, self.home, "tie") and not self.side.startswith("not_"):
                raise LegSpecError(f"moneyline side must be a team code or tie, got {self.side!r}")
        if self.team is not None and self.team not in (self.away, self.home):
            raise LegSpecError(f"team {self.team!r} not in game {self.away}@{self.home}")

    @property
    def game_key(self) -> str:
        return f"{self.sport}|{self.game_date}|{self.away}@{self.home}"

    @property
    def other_team(self) -> str | None:
        if self.team is None:
            return None
        return self.home if self.team == self.away else self.away

    def with_game_date(self, game_date: str) -> "LegSpec":
        return replace(self, game_date=game_date, game_date_basis="et")

    def as_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue, "sport": self.sport, "away": self.away, "home": self.home,
            "game_date": self.game_date, "game_date_basis": self.game_date_basis,
            "family": self.family, "period": self.period, "team": self.team, "line": self.line,
            "bucket": self.bucket, "side": self.side, "includes_ot": self.includes_ot,
            "canonical_key": canonical_key(self), "game_key": self.game_key,
            "venue_ids": dict(self.venue_ids), "rules": dict(self.rules),
        }


def _fmt_line(line: float | None) -> str:
    return "-" if line is None else f"{line:g}"


def canonical_key(spec: LegSpec) -> str:
    """Venue-independent contract identity (rules excluded).

    e.g. ``nfl|2026-10-25|IND@MIN|FG|spread|MIN|-7.5|MIN`` (Vikings cover -7.5),
         ``nfl|2026-10-25|CIN@BAL|1Q|total|-|5.5|over``,
         ``nfl|2026-10-25|CIN@BAL|FG|exact_margin|CIN:1-6|-|yes``.
    """
    subject = spec.team or "-"
    if spec.bucket is not None:
        subject = f"{spec.team}:{spec.bucket}" if spec.team else spec.bucket
    return "|".join([spec.sport, spec.game_date, f"{spec.away}@{spec.home}", spec.period,
                     spec.family, subject, _fmt_line(spec.line), spec.side])


def parse_canonical_key(key: str, *, venue: str = "polymarket", rules: dict[str, str] | None = None,
                        game_date_basis: str = "et") -> LegSpec:
    """Inverse of ``canonical_key`` (rules are not in the key; pass them or get none)."""
    parts = key.split("|")
    if len(parts) != 8:
        raise LegSpecError(f"bad canonical key {key!r}")
    sport, game_date, teams, period, family, subject, line_s, side = parts
    if "@" not in teams:
        raise LegSpecError(f"bad canonical key {key!r}")
    away, home = teams.split("@", 1)
    team: str | None = None
    bucket: str | None = None
    if subject != "-":
        if ":" in subject:
            team, bucket = subject.split(":", 1)
        elif subject in (away, home):
            team = subject
        else:
            bucket = subject
    line = None if line_s == "-" else float(line_s)
    return LegSpec(venue=venue, sport=sport, away=away, home=home, game_date=game_date, family=family, period=period,
                   side=side, team=team, line=line, bucket=bucket, game_date_basis=game_date_basis, rules=dict(rules or {}))


def contract_key(spec: LegSpec) -> str:
    """Identity of the *market* (both sides share it): canonical key without the side."""
    return canonical_key(spec).rsplit("|", 1)[0]


def complement_side(spec: LegSpec) -> str:
    """The side that pays exactly when ``spec.side`` does not, within the same market."""
    s = spec.side
    if s == "over":
        return "under"
    if s == "under":
        return "over"
    if s == "yes":
        return "no"
    if s == "no":
        return "yes"
    if spec.family == "spread" and s in (spec.away, spec.home):
        return spec.home if s == spec.away else spec.away
    kind = family_kind(spec.family)
    if isinstance(kind, tuple) and s in kind:
        return kind[1] if s == kind[0] else kind[0]
    if kind == "team":
        if s.startswith("not_"):
            return s[4:]
        if s in (spec.away, spec.home) and spec.rules.get("tie", "n/a") in ("50-50", "n/a"):
            return spec.home if s == spec.away else spec.away   # two-outcome market (tie splits or impossible)
        if s in (spec.away, spec.home, "tie"):
            return f"not_{s}"                                   # three-outcome market (tie strike)
    raise LegSpecError(f"complement undefined for {canonical_key(spec)}")


def et_date_candidates(spec: LegSpec) -> tuple[str, ...]:
    """Possible Eastern game dates: just ``game_date`` when its basis is ET; when the basis is
    ``utc_slug`` the slug (UTC) date and the day before (UTC date >= ET date, by at most one)."""
    if spec.game_date_basis == "et":
        return (spec.game_date,)
    d = date.fromisoformat(spec.game_date)
    return (d.isoformat(), (d - timedelta(days=1)).isoformat())


def complement(spec: LegSpec) -> LegSpec:
    """The other side of the same binary market. Raises LegSpecError when undefined."""
    return replace(spec, side=complement_side(spec))
