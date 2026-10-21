"""VERIFY step for subjective requirements + the executive summary.

"Clear explanation" can't be checked with an equality test. We collect every such message
during the run and ask a cheap model to grade them all in ONE batched call. Offline, a
transparent keyword heuristic is used instead and the report says so.
"""
from __future__ import annotations

import re

from .harness import RunState
from .llm import LLM
from .models import Requirement, Scenario

JUDGE_SYSTEM = """You are a strict UX/QA reviewer for a banking app. For each item decide whether the
message shown to the user is a CLEAR explanation of what went wrong: it must be specific to the
problem (not generic like "invalid request" or "something went wrong") and tell the user what is
wrong or what to do. Return only JSON: {"items": [{"i": <index>, "clear": true|false, "reason": "<= 20 words"}]}"""

GENERIC = {"error", "invalid", "the request is invalid.", "something went wrong",
           "some of the submitted information is invalid.", "the request could not be completed."}
DOMAIN = re.compile(r"amount|decimal|positive|zero|greater|balance|funds|insufficient|exceed|different|same|"
                    r"account|date|complete|moved|unchanged|start|end|try again", re.I)


def heuristic_clear(message: str) -> tuple[bool, str]:
    m = message.strip()
    core = re.sub(r"\s+", " ", m.lower())
    if not m:
        return False, "no message shown"
    if core in GENERIC or len(core) < 15:
        return False, "message is generic or too short"
    if not DOMAIN.search(core):
        return False, "message does not mention the field or the problem"
    return True, "specific message mentioning the problem (heuristic)"


def judge_all(run: RunState, llm: LLM) -> None:
    pending = []
    for j in run.judgements:
        if not j.message.strip():
            j.assertion.verdict = "failed"
            j.assertion.observed = "(no message shown to the user)"
        else:
            pending.append(j)
    if not pending:
        return
    listing = "\n".join(f"[{i}] Action: {j.action}\n    Expected: {j.expectation}\n    Message shown: {j.message[:400]!r}"
                        for i, j in enumerate(pending))
    result = llm.json("clarity_judge", JUDGE_SYSTEM, listing, max_tokens=60 + 40 * len(pending))
    verdicts = {}
    if isinstance(result, dict):
        verdicts = {int(x.get("i", -1)): x for x in result.get("items", []) if isinstance(x, dict)}
    for i, j in enumerate(pending):
        if i in verdicts:
            clear, reason = bool(verdicts[i].get("clear")), f"LLM judge: {verdicts[i].get('reason', '')}"
        else:
            clear, reason = heuristic_clear(j.message)
            reason = f"heuristic: {reason}"
        j.assertion.verdict = "passed" if clear else "failed"
        j.assertion.observed = f"{j.message[:300]} — {reason}"


SUMMARY_SYSTEM = """You are a QA lead writing for engineers. Be factual, concise and only use the
evidence given. Return only JSON: {"summary": "<4-6 sentences>",
"fixes": {"<scenario id>": "<likely root cause and fix, <= 30 words>"}}"""


def summarize(requirements: list[Requirement], scenarios: list[Scenario], llm: LLM) -> dict:
    failed = [s for s in scenarios if s.verdict == "failed"]
    counts = {v: sum(r.verdict == v for r in requirements) for v in ("passed", "failed", "unverified")}
    lines = []
    for s in failed:
        bad = [a for a in s.assertions if a.verdict == "failed"]
        lines.append(f"{s.id} ({s.title}): " + "; ".join(f"[{a.covers}] {a.statement} | expected {a.expected} | observed {str(a.observed)[:200]}"
                                                       for a in bad[:3]))
    prompt = f"Requirement verdicts: {counts}\nFailing scenarios:\n" + "\n".join(lines)
    result = llm.json("executive_summary", SUMMARY_SYSTEM, prompt, max_tokens=1500) if failed else None
    if isinstance(result, dict) and result.get("summary"):
        return {"summary": result["summary"], "fixes": result.get("fixes", {}), "source": "llm"}
    worst = ", ".join(f"{s.id}" for s in failed[:6])
    text = (f"{counts['passed']} requirements passed, {counts['failed']} failed and {counts['unverified']} could not be "
            f"verified. " + (f"Failing scenarios: {worst}. " if failed else "No failing scenarios. ") +
            "Each failure below lists reproduction steps, expected vs observed values and evidence.")
    return {"summary": text, "fixes": {}, "source": "template"}
