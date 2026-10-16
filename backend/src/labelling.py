"""
Who each stretch of a recording belongs to.

CMED's page sends a start for every profile the doctor clicks. While the
consultation has no prescription the recorder keeps recording and signs a
marker saying who was opened, at which second of audio - because at the click
nobody can tell a glance at another file, a wrong profile being corrected, and
the next patient after a follow-up that needed no prescription. They differ
only in what the doctor does afterwards, so the decision is taken here, once
the recording has closed and every signal is in.

    R1  a patient with a prescription (3a in the recording, or a 3b on this
        server) was consulted; their stretches are never given away
    R2  Z, then other patients, then Z again: the doctor went back, so what was
        between - unless prescribed - was a glance and is Z's
    R3  an unprescribed stretch under a minute whose patient is not opened
        again was a wrong pick or a stray click: it joins the stretch after it
    R4  an unprescribed stretch of a minute or more that survived the rules
        above is a real consultation without a prescription (a follow-up)
    R5  what the rules cannot settle is kept as they leave it and flagged for a
        person: never deleted, never guessed silently

Pure: no database, no clock. Tested scenario by scenario in
tests/test_labelling.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set

# Shorter than this, unprescribed and never reopened: a wrong pick (R3).
SHORT_SECONDS = 60.0

OPEN = "open"
PATIENT_OPENED = "patient_opened"
PRESCRIPTION_BUILT = "prescription_built"


@dataclass(frozen=True)
class Visit:
    """The five fields CMED sends with every message for one visit."""
    patient_id: str
    doctor_id: str
    cmed_hospital_id: str
    start_time: str
    visit_date: str


@dataclass(frozen=True)
class Event:
    """One focus event: the recording's open, a click, or a prescription."""
    seconds: float
    kind: str
    visit: Visit
    at: Optional[datetime] = None
    grant_jti: Optional[str] = None
    entry_no: int = 0


@dataclass
class Part:
    part_no: int
    start_seconds: float
    end_seconds: Optional[float]            # None: to the end of the recording
    visit: Visit
    grant_jti: Optional[str]
    rule: str
    needs_review: bool
    start_at: Optional[datetime]            # wall clock of the event that began it
    end_at: Optional[datetime]              # of the event that began the next part

    @property
    def patient_id(self) -> str:
        return self.visit.patient_id

    def layout(self):
        """What a cut depends on: a change here means the files must be cut again."""
        return (self.part_no, round(self.start_seconds, 3),
                None if self.end_seconds is None else round(self.end_seconds, 3),
                self.visit.patient_id)


@dataclass
class _Span:
    start: float
    end: float
    owner: str
    start_at: Optional[datetime]
    rules: Set[str] = field(default_factory=set)
    protected: bool = False

    @property
    def length(self) -> float:
        return self.end - self.start


# Merges are written before the verdict they led to: "R3+R1".
_ORDER = ("R2", "R3", "R1", "R4", "R5")


def _rule_text(rules: Iterable[str]) -> str:
    present = set(rules)
    return "+".join(r for r in _ORDER if r in present)


def resolve(events: Sequence[Event], *, total_seconds: float,
            prescribed: Iterable[str] = (),
            closed_at: Optional[datetime] = None) -> List[Part]:
    """
    The parts of one closed recording.

    `events` must include the open (seconds 0, the first patient). `prescribed`
    is every patient_id with a prescription on this server; prescription_built
    markers in `events` count too. `total_seconds` is the joined audio length.
    """
    ordered = sorted(events, key=lambda e: (e.seconds, e.entry_no))
    if not ordered:
        raise ValueError("a recording has at least its open")
    total = max(float(total_seconds), ordered[-1].seconds, 0.0)

    rx: Set[str] = set(prescribed) | {
        e.visit.patient_id for e in ordered if e.kind == PRESCRIPTION_BUILT}

    # The first event for each patient names them: its five fields and grant.
    first: Dict[str, Event] = {}
    grants: Dict[str, Optional[str]] = {}
    for e in ordered:
        pid = e.visit.patient_id
        first.setdefault(pid, e)
        if grants.get(pid) is None and e.grant_jti:
            grants[pid] = e.grant_jti

    # 1. Stretches, split wherever the doctor's focus moved.
    spans: List[_Span] = []
    for e in ordered:
        at = min(max(e.seconds, 0.0), total)
        pid = e.visit.patient_id
        if spans and spans[-1].owner == pid:
            continue
        if spans:
            spans[-1].end = at
        spans.append(_Span(start=at, end=total, owner=pid, start_at=e.at))
    spans[0].start = 0.0
    spans = [s for s in spans if s.length > 0] or [spans[-1]]

    # R1 first: a prescribed patient's audio is never given away.
    for s in spans:
        if s.owner in rx:
            s.protected = True
            s.rules.add("R1")

    # 2. R2, left to right: going back to an earlier patient takes back the
    # unprescribed stretches in between.
    out: List[_Span] = []
    for s in spans:
        if out and out[-1].owner == s.owner:
            out[-1].end = s.end
            out[-1].rules |= s.rules
            continue
        back = next((k for k in range(len(out) - 1, -1, -1) if out[k].owner == s.owner),
                    None)
        if back is not None and all(not o.protected for o in out[back + 1:]):
            keep = out[back]
            keep.end = s.end
            keep.rules |= s.rules | {"R2"}
            del out[back + 1:]
            continue
        out.append(s)

    # 3. R3: a short unprescribed stretch, never reopened, joins the next one.
    i = 0
    while i < len(out) - 1:
        s = out[i]
        later = any(o.owner == s.owner for o in out[i + 1:])
        if not s.protected and s.length < SHORT_SECONDS and not later:
            nxt = out[i + 1]
            nxt.start = s.start
            nxt.start_at = s.start_at
            nxt.rules |= {r for r in s.rules if r != "R1"} | {"R3"}
            del out[i]
            continue
        i += 1

    # Joining adjacent stretches of one patient again, after R3.
    joined: List[_Span] = []
    for s in out:
        if joined and joined[-1].owner == s.owner:
            joined[-1].end = s.end
            joined[-1].rules |= s.rules
            joined[-1].protected |= s.protected
        else:
            joined.append(s)

    # 4. R4 and R5.
    review: Set[int] = set()
    seen: Dict[str, int] = {}
    for n, s in enumerate(joined):
        if not s.protected:
            s.rules.add("R4")
        if s.owner in seen:                          # one patient, two parts
            review |= {seen[s.owner], n}
        seen.setdefault(s.owner, n)
    last = joined[-1]
    if len(joined) > 1 and not last.protected and last.length < SHORT_SECONDS:
        review.add(len(joined) - 1)                   # a short stretch at the end
    for n in review:
        joined[n].rules.add("R5")

    parts: List[Part] = []
    for n, s in enumerate(joined):
        nxt = joined[n + 1] if n + 1 < len(joined) else None
        parts.append(Part(
            part_no=n + 1,
            start_seconds=round(s.start, 3),
            end_seconds=None if nxt is None else round(s.end, 3),
            visit=first[s.owner].visit,
            grant_jti=grants.get(s.owner),
            rule=_rule_text(s.rules),
            needs_review=n in review,
            start_at=s.start_at,
            end_at=nxt.start_at if nxt is not None else closed_at,
        ))
    return parts


def same_cut(old: Sequence[Part], new: Sequence[Part]) -> bool:
    """True when the files already cut still stand (only the rule text moved)."""
    return [p.layout() for p in old] == [p.layout() for p in new]


def with_times(part: Part, *, start_at: Optional[datetime],
               end_at: Optional[datetime]) -> Part:
    return replace(part, start_at=start_at, end_at=end_at)


__all__ = ["Visit", "Event", "Part", "resolve", "same_cut", "SHORT_SECONDS",
           "OPEN", "PATIENT_OPENED", "PRESCRIPTION_BUILT"]
