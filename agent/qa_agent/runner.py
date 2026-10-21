"""Orchestrates the QA loop: INTERPRET → PLAN → EXECUTE → VERIFY → REPORT."""
from __future__ import annotations

import importlib
import json
import time
from datetime import datetime
from pathlib import Path

from .browser import Driver
from .flows import api_state, enter_demo, restore_balances
from .harness import REGISTRY, Ctx, RunState
from .judge import judge_all, summarize
from .llm import LLM
from .models import Evidence, Scenario, roll_up
from .prd import load_requirements, resolve_anchor
from .report import write_reports

SUITES = ["session", "accounts", "history", "profile_ui", "transfers"]


def run(*, url: str, prd: Path, out_root: Path, offline: bool = False, headed: bool = False,
        only: list[str] | None = None, api_url: str | None = None, chaos: bool = False,
        fast_model: str | None = None, smart_model: str | None = None, provider: str = "auto") -> Path:
    started = time.time()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = out_root / f"run-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    run_state = RunState(run_id=stamp[-6:])

    # 1. INTERPRET -----------------------------------------------------------
    llm = LLM(out_root / ".llm-cache", provider=provider, offline=offline, fast_model=fast_model,
              smart_model=smart_model)
    requirements = load_requirements(prd, llm, out)
    print(f"[interpret] {len(requirements)} requirements extracted from {prd.name}"
          f" ({'LLM-enriched via ' + llm.provider + ' / ' + llm.smart_model if llm.available else 'deterministic parser only (no API key / offline)'})")

    # 2. PLAN ------------------------------------------------------------------
    for name in SUITES:
        importlib.import_module(f"qa_agent.suites.{name}")
    specs = sorted(REGISTRY, key=lambda s: (s.order, s.id))
    if only:
        specs = [s for s in specs if any(s.suite == o or s.id.startswith(o) for o in only)]
    for s in specs:
        for a in s.anchors:
            if resolve_anchor(a, requirements) is None:
                print(f"[plan] warning: anchor not found in PRD: {a!r} ({s.id})")
    print(f"[plan] {len(specs)} scenarios: "
          + ", ".join(f"{k}={sum(s.kind == k for s in specs)}" for k in ("positive", "negative", "edge")))

    # 3. EXECUTE + VERIFY -----------------------------------------------------
    driver = Driver(url, out, llm, headless=not headed, api_base=api_url, chaos=chaos)
    driver.start()
    scenarios: list[Scenario] = []
    # Preflight: fail fast with a clear message instead of timing out 40 scenarios one by one.
    try:
        driver.new_context()
        probe = Ctx(driver, llm, requirements, Scenario("PREFLIGHT", "preflight", "positive", []), run_state)
        if not enter_demo(probe):
            raise RuntimeError("the landing page loaded but 'Enter Demo' did not reach the dashboard")
        status, me = driver.api_get("/api/me")
        if status == 200 and isinstance(me, dict) and me.get("display_name"):
            print(f"[preflight] target reachable; API discovered at {driver.api_base} (verified)")
        else:
            print(f"[preflight] WARNING: could not verify the app's API at {driver.api_base!r} (HTTP {status}). "
                  "Balance/record checks will be 'unverified'. Pass --api-url <backend-url> "
                  "(local DemoBank: http://localhost:8000).")
    except Exception as exc:
        driver.stop()
        raise SystemExit(f"[preflight] target not usable at {url}: {str(exc).splitlines()[0]}")
    try:
        for spec in specs:
            sc = _execute(spec, driver, llm, requirements, run_state)
            scenarios.append(sc)
            pending = any(j.assertion in sc.assertions for j in run_state.judgements)
            verdict = sc.verdict if not (pending and sc.verdict == "unverified") else "judging"
            mark = {"passed": "✔", "failed": "✘", "unverified": "?", "judging": "…"}[verdict]
            print(f"  {mark} {sc.id:<20} {verdict:<10} {sc.duration_s:5.1f}s  {sc.title}")
    finally:
        driver.stop()

    judge_all(run_state, llm)
    for note in llm.notes:
        print(f"[llm] {note}")
    print(f"[verify] {len(run_state.judgements)} message-clarity judgements in one batch "
          f"({'LLM' if llm.available else 'heuristic'})")
    for sc in scenarios:
        if any(j.assertion in sc.assertions for j in run_state.judgements):
            print(f"  → {sc.id:<20} {sc.verdict}")
    roll_up(requirements, scenarios)
    summary = summarize(requirements, scenarios, llm)

    # 4. REPORT ---------------------------------------------------------------
    meta = {
        "target_url": url, "api_base": driver.api_base, "prd": str(prd), "run_id": run_state.run_id,
        "started": datetime.fromtimestamp(started).isoformat(timespec="seconds"),
        "duration_s": round(time.time() - started, 1), "llm_mode": llm.provider if llm.available else "offline",
        "models": {"fast": llm.fast_model, "smart": llm.smart_model}, "browser_actions": driver.actions, "chaos": chaos,
        "api_events_captured": len(driver.net), "heals": driver.heals,
        "compensations": run_state.ledger_moves, "unmapped_anchors": sorted(run_state.unmapped),
        "llm_notes": llm.notes,
    }
    write_reports(out, requirements, scenarios, llm.ledger, summary, meta)
    counts = {v: sum(r.verdict == v for r in requirements) for v in ("passed", "failed", "unverified")}
    print(f"[report] requirements {counts} | LLM cost ${llm.ledger.total_usd:.4f} ({llm.ledger.provider} list price)"
          f" | Claude-equivalent ${llm.ledger.total_claude_equiv:.4f} | {meta['duration_s']}s")
    print(f"[report] {out / 'report.html'}")
    return out


def _execute(spec, driver: Driver, llm: LLM, requirements, run_state: RunState) -> Scenario:
    """Run one scenario in a fresh browser profile, retrying once if it is safe to do so."""
    t0 = time.time()
    sc = Scenario(spec.id, spec.title, spec.kind, spec.anchors)
    for attempt in (1, 2):
        sc = Scenario(spec.id, spec.title, spec.kind, spec.anchors, attempts=attempt)
        driver.new_context()
        ctx = Ctx(driver, llm, requirements, sc, run_state)
        posts_before = sum(1 for e in driver.net if e.method == "POST" and "/transfers" in e.url)
        before = None
        try:
            if spec.needs_session:
                ctx.step("Open the app in a fresh browser profile and click 'Enter Demo'")
                if not enter_demo(ctx):
                    raise RuntimeError("could not enter the demo session")
            if spec.mutates:
                before = api_state(ctx)
            spec.fn(ctx)
            break
        except Exception as exc:  # resilience: one broken scenario never stops the run
            sc.error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}"
            sc.evidence.append(driver.screenshot(f"{spec.id} error attempt {attempt}"))
            mutated = sum(1 for e in driver.net if e.method == "POST" and "/transfers" in e.url) > posts_before
            if attempt == 1 and not mutated:
                ctx.step(f"Attempt 1 failed ({sc.error}); retrying in a fresh profile")
                continue
            for anchor in spec.anchors:
                ctx.unverified(anchor, f"{spec.title} (scenario aborted)", sc.error)
        finally:
            if spec.mutates and before is not None and before.ok:
                try:
                    note = restore_balances(ctx, before)
                    sc.evidence.append(Evidence("note", "Test hygiene", note))
                except Exception as exc:
                    sc.evidence.append(Evidence("note", "Test hygiene", f"restore failed: {exc}"))
    sc.duration_s = round(time.time() - t0, 1)
    return sc
