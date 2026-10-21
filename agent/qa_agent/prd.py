"""Step 1 of the QA loop: INTERPRET the PRD into structured, testable requirements.

Two layers:

1. A deterministic Markdown parser turns every bullet (and every "must/should" sentence in
   prose sections) into a Requirement with a stable ID like TRF-04. Same PRD in, same IDs out,
   which makes runs comparable and costs nothing.
2. An optional LLM pass decomposes each requirement into atomic assertions and suggests
   normal / negative / edge scenarios. Those ideas appear in the report, and any requirement
   that no scenario covers is reported as *unverified* together with what the model would test.

Scenarios don't hard-code IDs. They declare an *anchor phrase* ("positive number greater than
zero") that is resolved to whichever requirement contains it. If the PRD is reordered,
the mapping still holds.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .llm import LLM
from .models import Requirement

SECTION_PREFIX = {
    "purpose": "GEN",
    "users and access": "SES",
    "dashboard and accounts": "DSH",
    "transfers between owned accounts": "TRF",
    "transaction history": "HIS",
    "profile": "PRF",
    "general interface behavior": "UI",
    "seed data expectations": "SEED",
}
SKIP_SECTIONS = {"out of scope", "requirement coverage expectations"}
NORMATIVE = re.compile(r"\b(must|should|can|shows?|displays?|provides?|includes?|has|begins)\b", re.I)


def _sentences(paragraph: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", paragraph) if s.strip()]


def parse_prd(markdown: str) -> list[Requirement]:
    reqs: list[Requirement] = []
    section = ""
    counters: dict[str, int] = {}
    paragraph: list[str] = []

    def add(text: str) -> None:
        prefix = SECTION_PREFIX.get(section.lower())
        if not prefix:
            return
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text).strip()
        counters[prefix] = counters.get(prefix, 0) + 1
        reqs.append(Requirement(id=f"{prefix}-{counters[prefix]:02d}", section=section, text=text))

    def flush_paragraph() -> None:
        if paragraph and section.lower() not in SKIP_SECTIONS:
            for sentence in _sentences(" ".join(paragraph)):
                # In prose sections keep only normative sentences ("must", "should", ...).
                if NORMATIVE.search(sentence) and "behavioral contract" not in sentence.lower():
                    if section.lower() == "purpose" and "must" not in sentence.lower():
                        continue
                    add(sentence)
        paragraph.clear()

    for line in markdown.splitlines():
        if line.startswith("## "):
            flush_paragraph()
            section = line[3:].strip()
        elif line.startswith("# "):
            continue
        elif re.match(r"^\s*[-*]\s+", line):
            flush_paragraph()
            if section.lower() not in SKIP_SECTIONS:
                add(re.sub(r"^\s*[-*]\s+", "", line))
        elif line.strip():
            paragraph.append(line.strip())
        else:
            flush_paragraph()
    flush_paragraph()
    return reqs


ENRICH_SYSTEM = """You are a senior QA engineer. You turn product requirements into precise,
verifiable test assertions for a browser-based QA agent. Be concrete: name inputs, expected
visible outcomes and side effects to verify (balances, records). Return only JSON."""


def enrich(requirements: list[Requirement], llm: LLM) -> None:
    """One batched LLM call: atomic assertions + test ideas per requirement (cached on disk)."""
    listing = "\n".join(f"{r.id}: {r.text}" for r in requirements)
    prompt = (
        "For each requirement below return an object keyed by requirement ID with:\n"
        '  "atomic": list of short, independently checkable assertions (max 4)\n'
        '  "test_ideas": list of scenarios, each prefixed with [positive], [negative] or [edge] (max 4)\n'
        "Requirements:\n" + listing
    )
    result = llm.json("prd_enrichment", ENRICH_SYSTEM, prompt, smart=True, max_tokens=8000)
    if not isinstance(result, dict):
        return
    for r in requirements:
        item = result.get(r.id) or {}
        r.atomic = [str(x) for x in item.get("atomic", [])][:4]
        r.test_ideas = [str(x) for x in item.get("test_ideas", [])][:4]


def load_requirements(prd_path: Path, llm: LLM, out_dir: Path) -> list[Requirement]:
    reqs = parse_prd(prd_path.read_text(encoding="utf-8"))
    enrich(reqs, llm)
    (out_dir / "requirements.json").write_text(json.dumps([r.__dict__ for r in reqs], indent=2), encoding="utf-8")
    return reqs


def resolve_anchor(anchor: str, requirements: list[Requirement]) -> str | None:
    """Map an anchor phrase to a requirement ID (case/whitespace-insensitive substring match)."""
    needle = re.sub(r"\s+", " ", anchor.lower()).strip()
    for r in requirements:
        if needle in re.sub(r"\s+", " ", r.text.lower()):
            return r.id
    return None
