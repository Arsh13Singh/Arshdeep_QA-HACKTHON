"""Core data model.

The whole agent revolves around four nouns:

* Requirement – one behaviour promised by the PRD (e.g. "same-account transfers are rejected").
* Scenario    – something the agent *does* in the browser (normal, negative or edge case).
* Assertion   – one expected-vs-observed comparison made during a scenario, tied to a requirement.
* Evidence    – proof attached to a scenario: screenshots, observed values, network responses.

A requirement's final verdict is derived from all assertions that cover it, never from
"the click didn't throw". That is the key idea judges look for.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

Verdict = Literal["passed", "failed", "unverified"]
ScenarioKind = Literal["positive", "negative", "edge"]


@dataclass
class Requirement:
    id: str                      # stable ID such as "TRF-04"
    section: str                 # PRD section heading
    text: str                    # verbatim PRD sentence/bullet
    atomic: list[str] = field(default_factory=list)       # LLM-decomposed sub-assertions (optional)
    test_ideas: list[str] = field(default_factory=list)   # LLM-suggested scenarios (optional)
    verdict: Verdict = "unverified"
    verdict_reason: str = ""


@dataclass
class Evidence:
    kind: Literal["screenshot", "value", "network", "note"]
    label: str
    value: str                   # file path for screenshots, text for everything else


@dataclass
class Assertion:
    covers: str                  # requirement ID after resolution
    statement: str               # what we checked, in plain English
    verdict: Verdict
    expected: str = ""
    observed: str = ""


@dataclass
class Scenario:
    id: str
    title: str
    kind: ScenarioKind
    anchors: list[str]           # PRD phrases this scenario targets (resolved to requirement IDs)
    steps: list[str] = field(default_factory=list)
    assertions: list[Assertion] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    error: str = ""
    duration_s: float = 0.0
    attempts: int = 1

    @property
    def verdict(self) -> Verdict:
        if any(a.verdict == "failed" for a in self.assertions):
            return "failed"
        if self.assertions and all(a.verdict == "passed" for a in self.assertions):
            return "passed"
        return "unverified"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verdict"] = self.verdict
        return d


def roll_up(requirements: list[Requirement], scenarios: list[Scenario]) -> None:
    """Derive each requirement's verdict from the assertions that cover it.

    failed      – at least one assertion failed (evidence of a real mismatch)
    passed      – at least one assertion passed and none failed or were unverified
    unverified  – nothing reliable was observed (no check, or the check could not complete)
    """
    by_req: dict[str, list[tuple[Scenario, Assertion]]] = {}
    for sc in scenarios:
        for a in sc.assertions:
            by_req.setdefault(a.covers, []).append((sc, a))

    for req in requirements:
        items = by_req.get(req.id, [])
        fails = [(s, a) for s, a in items if a.verdict == "failed"]
        passes = [(s, a) for s, a in items if a.verdict == "passed"]
        unver = [(s, a) for s, a in items if a.verdict == "unverified"]
        if fails:
            req.verdict = "failed"
            req.verdict_reason = f"{len(fails)} failing assertion(s), e.g. {fails[0][1].statement}"
        elif passes and not unver:
            req.verdict = "passed"
            req.verdict_reason = f"{len(passes)} assertion(s) passed across {len({s.id for s, _ in passes})} scenario(s)"
        elif passes:
            req.verdict = "unverified"
            req.verdict_reason = (
                f"{len(passes)} passed but {len(unver)} could not be verified "
                f"(e.g. {unver[0][1].observed or unver[0][1].statement})"
            )
        elif unver:
            req.verdict = "unverified"
            req.verdict_reason = unver[0][1].observed or "check could not complete"
        else:
            req.verdict = "unverified"
            req.verdict_reason = "no automated check covers this requirement"
