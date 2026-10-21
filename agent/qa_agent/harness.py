"""Scenario registry and the per-scenario context object (`ctx`).

A scenario is a plain function decorated with @scenario. It receives a Ctx that lets it act in
the browser (ctx.d), record steps/evidence, and make assertions tied to PRD anchors:

    @scenario("TRF-NEG-ZERO", "Reject a zero amount", kind="negative",
              anchors=["positive number greater than zero"], mutates=True)
    def zero_amount(ctx):
        ...
        ctx.check("positive number greater than zero", "Zero amount is rejected",
                  ok=not outcome.success, expected="validation message", observed=outcome.summary)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .browser import Driver
from .llm import LLM
from .models import Assertion, Evidence, Requirement, Scenario
from .prd import resolve_anchor


@dataclass
class ScenarioSpec:
    id: str
    title: str
    kind: str
    anchors: list[str]
    fn: Callable
    mutates: bool = False
    order: int = 50
    suite: str = ""
    needs_session: bool = True


REGISTRY: list[ScenarioSpec] = []


def scenario(id: str, title: str, *, kind: str, anchors: list[str], mutates: bool = False,
             order: int = 50, needs_session: bool = True):
    """Register a scenario. `needs_session` => the runner enters the demo before calling it."""
    def wrap(fn: Callable) -> Callable:
        REGISTRY.append(ScenarioSpec(id, title, kind, anchors, fn, mutates, order,
                                     suite=fn.__module__.rsplit(".", 1)[-1],
                                     needs_session=needs_session))
        return fn
    return wrap


@dataclass
class PendingJudgement:
    assertion: Assertion
    requirement: str
    action: str
    message: str
    expectation: str


@dataclass
class RunState:
    run_id: str
    judgements: list[PendingJudgement] = field(default_factory=list)
    ledger_moves: list[dict] = field(default_factory=list)   # every balance-changing action we caused
    unmapped: set[str] = field(default_factory=set)


class Ctx:
    def __init__(self, driver: Driver, llm: LLM, requirements: list[Requirement],
                 sc: Scenario, run: RunState) -> None:
        self.d = driver
        self.llm = llm
        self.requirements = requirements
        self.sc = sc
        self.run = run
        self.memo_seq = 0

    # -- bookkeeping ---------------------------------------------------------
    def step(self, text: str) -> None:
        self.sc.steps.append(text)

    def shot(self, label: str) -> Evidence:
        ev = self.d.screenshot(f"{self.sc.id} {label}")
        self.sc.evidence.append(ev)
        return ev

    def value(self, label: str, value) -> None:
        self.sc.evidence.append(Evidence("value", label, str(value)))

    def network(self, label: str, ev) -> None:
        if ev is not None:
            self.sc.evidence.append(Evidence("network", label,
                                             f"{ev.method} {ev.url} -> {ev.status}\n{ev.body[:1500]}"))

    def memo(self, tag: str) -> str:
        """Unique memo so we can find exactly the record this scenario created."""
        self.memo_seq += 1
        return f"QA-{self.run.run_id}-{self.sc.id}-{tag}{self.memo_seq}"

    def _req_id(self, anchor: str) -> str:
        rid = resolve_anchor(anchor, self.requirements)
        if rid is None:
            self.run.unmapped.add(anchor)
            return f"UNMAPPED:{anchor[:40]}"
        return rid

    # -- assertions ----------------------------------------------------------
    def check(self, anchor: str, statement: str, ok: bool | None, expected: str = "",
              observed: str = "") -> bool:
        verdict = "unverified" if ok is None else ("passed" if ok else "failed")
        self.sc.assertions.append(Assertion(self._req_id(anchor), statement, verdict,
                                            str(expected), str(observed)))
        return bool(ok)

    def unverified(self, anchor: str, statement: str, reason: str) -> None:
        self.check(anchor, statement, None, observed=reason)

    def judge_clarity(self, anchor: str, statement: str, action: str, message: str,
                      expectation: str) -> None:
        """Queue a subjective judgement ("is this message a clear explanation?").

        All judgements are sent to the model in ONE batched call after the run, which is
        far cheaper than one call per message.
        """
        a = Assertion(self._req_id(anchor), statement, "unverified",
                      expected=expectation, observed=message or "(no message shown)")
        self.sc.assertions.append(a)
        self.run.judgements.append(PendingJudgement(a, a.covers, action, message, expectation))
