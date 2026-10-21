"""REPORT step: requirement-level verdicts, defects with repro steps, evidence and cost.

Three outputs from the same data:
* report.json – machine-readable (CI, diffing runs)
* report.md   – renders on GitHub; good for the submission repo
* report.html – self-contained audit for judges (failure screenshots embedded inline)
"""
from __future__ import annotations

import base64
import html
import json
from pathlib import Path

from .llm import Ledger
from .models import Requirement, Scenario

PILL = {"passed": "PASS", "failed": "FAIL", "unverified": "UNVERIFIED"}


def write_reports(out: Path, reqs: list[Requirement], scenarios: list[Scenario], ledger: Ledger,
                  summary: dict, meta: dict) -> None:
    data = {
        "meta": meta, "summary": summary,
        "counts": {v: sum(r.verdict == v for r in reqs) for v in PILL},
        "requirements": [r.__dict__ for r in reqs],
        "scenarios": [s.to_dict() for s in scenarios],
        "cost": ledger.to_dict(),
    }
    (out / "report.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    (out / "report.md").write_text(_markdown(data), encoding="utf-8")
    (out / "report.html").write_text(_html(data, out), encoding="utf-8")


# ---------------------------------------------------------------------- helpers
SEVERITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}


def severity(failed: list[dict]) -> str:
    """Simple, explainable triage rule (documented in the README):
    Critical – money moved or records created when they must not be (financial integrity)
    High     – transfer/session/privacy rules violated without corrupting balances
    Medium   – history/filter/display data is wrong or missing
    Low      – wording, layout and other UX issues
    """
    text = " ".join(a["statement"].lower() for a in failed)
    covers = {a["covers"].split("-")[0] for a in failed}
    if "balances are unchanged" in text or "no transaction record" in text or "exactly one transfer" in text:
        return "Critical"
    if covers & {"TRF", "SES", "PRF"}:
        return "High"
    if covers & {"HIS", "DSH", "SEED"}:
        return "Medium"
    return "Low"


def _defects(data: dict) -> list[dict]:
    """One defect per failed scenario, carrying only its failed assertions, most severe first."""
    out = []
    for s in data["scenarios"]:
        bad = [a for a in s["assertions"] if a["verdict"] == "failed"]
        if bad:
            out.append({**s, "failed": bad, "severity": severity(bad)})
    return sorted(out, key=lambda s: (SEVERITY_ORDER[s["severity"]], s["id"]))


def _cost_rows(data: dict) -> list[dict]:
    rows = data["cost"]["by_purpose"]
    if not rows and data["cost"].get("estimates"):
        agg: dict[str, dict] = {}
        for e in data["cost"]["estimates"]:
            r = agg.setdefault(e["purpose"], {"purpose": e["purpose"] + " (estimated)", "model": e["model"], "calls": 0,
                                              "cached": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0})
            r["calls"] += 1
            r["input_tokens"] += e["input_tokens"]
            r["output_tokens"] += e["output_tokens"]
            r["usd"] = round(r["usd"] + e["usd"], 6)
        rows = list(agg.values())
    return rows


def _fmt(v) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)[:600]
    return str(v)


# ---------------------------------------------------------------------- markdown
def _markdown(d: dict) -> str:
    m, c = d["meta"], d["counts"]
    L = [f"# DemoBank QA audit report", "",
         f"**Target:** {m['target_url']}  |  **Run:** {m['started']}  |  **Duration:** {m['duration_s']} s  |  "
         f"**LLM mode:** {m['llm_mode']}", "",
         f"| Passed | Failed | Unverified | Scenarios | LLM cost |", "|---|---|---|---|---|",
         f"| {c['passed']} | {c['failed']} | {c['unverified']} | {len(d['scenarios'])} | ${d['cost']['total_usd']:.4f} |", "",
         "## Executive summary", "", d["summary"]["summary"], "", "## Requirement coverage", "",
         "| ID | Requirement | Verdict | Reason |", "|---|---|---|---|"]
    for r in d["requirements"]:
        L.append(f"| {r['id']} | {r['text'][:140].replace('|', '/')} | **{PILL[r['verdict']]}** | {r['verdict_reason'][:160].replace('|', '/')} |")
    L += ["", "## Defects", ""]
    for i, s in enumerate(_defects(d), 1):
        reqs = sorted({a["covers"] for a in s["failed"]})
        L += [f"### D{i}. [{s['severity']}] {s['title']}  (`{s['id']}`, {s['kind']})", "", f"**Requirements:** {', '.join(reqs)}", "",
              "**Steps to reproduce**", ""] + [f"{n}. {st}" for n, st in enumerate(s["steps"], 1)] + [""]
        for a in s["failed"]:
            L += [f"- **{a['statement']}**  ", f"  Expected: {_fmt(a['expected'])}  ", f"  Observed: {_fmt(a['observed'])}"]
        fix = d["summary"].get("fixes", {}).get(s["id"])
        if fix:
            L += ["", f"*Likely cause / fix:* {fix}"]
        shots = [e["value"] for e in s["evidence"] if e["kind"] == "screenshot"]
        L += [""] + [f"![{Path(p).name}]({p})" for p in shots[:2]] + [""]
    L += ["## Cost and efficiency", "", f"Provider: {d['cost'].get('provider')}. Claude-equivalent prices the same token counts "
          "on Claude Haiku 5.5 / Sonnet 5.5.", "",
          "| Purpose | Model | Calls | Cached | In tokens | Out tokens | USD (provider) | USD (Claude-equiv.) |",
          "|---|---|---|---|---|---|---|---|"]
    for r in _cost_rows(d):
        L.append(f"| {r['purpose']} | {r['model']} | {r['calls']} | {r['cached']} | {r['input_tokens']} | {r['output_tokens']} "
                 f"| ${r['usd']:.4f} | ${r.get('claude_equiv_usd', r['usd']):.4f} |")
    L += ["", f"Browser actions: {m['browser_actions']}. API responses captured: {m['api_events_captured']}. "
              f"Locator heals: {len(m['heals'])}. Fallback steps: {', '.join(d['cost']['fallbacks']) or 'none'}.", ""]
    return "\n".join(L)


# ---------------------------------------------------------------------- html
CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e4e3de;--pass:#1f7a4d;--passbg:#e3f3ea;
--fail:#b42318;--failbg:#fde8e6;--unv:#8a5a00;--unvbg:#fdf1d8;--accent:#2f4bd8}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--mute:#a19f97;--line:#33322e;
--pass:#5fd39a;--passbg:#163626;--fail:#ff8a7a;--failbg:#3a1714;--unv:#f3c25f;--unvbg:#3a2c0e;--accent:#8ea2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:32px 20px 80px}h1{font-size:28px;margin:0 0 4px}h2{font-size:20px;margin:40px 0 12px}
.meta{color:var(--mute);font-size:13px}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:24px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}.tile b{display:block;font-size:26px}
.tile span{color:var(--mute);font-size:13px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin:12px 0}
table{width:100%;border-collapse:collapse;font-size:13.5px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:600}.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:700;letter-spacing:.03em}
.passed{background:var(--passbg);color:var(--pass)}.failed{background:var(--failbg);color:var(--fail)}.unverified{background:var(--unvbg);color:var(--unv)}
.kind{font-size:12px;color:var(--mute);border:1px solid var(--line);border-radius:6px;padding:1px 6px;margin-left:6px}
.eo{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:8px 0 14px}.eo div{background:var(--bg);border-radius:8px;padding:10px 12px;font-size:13px;overflow-wrap:anywhere}
.eo small{display:block;color:var(--mute);font-weight:600;margin-bottom:2px}ol{padding-left:20px}code,pre{font:12.5px ui-monospace,Menlo,monospace}
pre{white-space:pre-wrap;background:var(--bg);padding:10px;border-radius:8px;max-height:260px;overflow:auto}
.shots{display:flex;gap:10px;flex-wrap:wrap}.shots img{max-width:360px;width:100%;border:1px solid var(--line);border-radius:8px}
details{margin:6px 0}summary{cursor:pointer;font-weight:600}.fix{border-left:3px solid var(--accent);padding:6px 12px;margin:10px 0;font-size:13.5px}
@media (max-width:700px){.eo{grid-template-columns:1fr}}
"""


def _esc(v) -> str:
    return html.escape(_fmt(v))


def _img(out: Path, rel: str, embed: bool) -> str:
    p = out / rel
    if embed and p.exists():
        src = "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()
    else:
        src = html.escape(rel)
    return f'<a href="{html.escape(rel)}"><img loading="lazy" src="{src}" alt="{html.escape(p.name)}"></a>'


def _html(d: dict, out: Path) -> str:
    m, c = d["meta"], d["counts"]
    cost = d["cost"]
    est = cost.get("estimated_usd_if_enabled", 0)
    if m["llm_mode"] == "offline":
        cost_tile, cost_label = f"~${est:.4f}", "LLM cost on Claude (estimated; ran offline)"
    elif m["llm_mode"] == "groq":
        cost_tile = f"${cost['total_claude_equiv_usd']:.4f}"
        cost_label = f"Claude-equivalent cost · Groq list price ${cost['total_usd']:.4f} (free tier: $0 billed)"
    else:
        cost_tile, cost_label = f"${cost['total_usd']:.4f}", "LLM cost (actual, Claude)"
    H = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>DemoBank QA Audit</title><style>{CSS}</style></head><body><div class=wrap>",
         "<h1>DemoBank QA audit</h1>",
         f"<div class=meta>Target <b>{html.escape(m['target_url'])}</b> · API {html.escape(str(m['api_base']))} · "
         f"{m['started']} · {m['duration_s']} s · LLM {m['llm_mode']} ({m['models']['fast']} / {m['models']['smart']})</div>",
         "<div class=tiles>",
         f"<div class=tile><b style='color:var(--pass)'>{c['passed']}</b><span>requirements passed</span></div>",
         f"<div class=tile><b style='color:var(--fail)'>{c['failed']}</b><span>requirements failed</span></div>",
         f"<div class=tile><b style='color:var(--unv)'>{c['unverified']}</b><span>unverified</span></div>",
         f"<div class=tile><b>{len(d['scenarios'])}</b><span>scenarios · {sum(len(s['assertions']) for s in d['scenarios'])} assertions</span></div>",
         f"<div class=tile><b>{cost_tile}</b><span>{cost_label}</span></div></div>",
         f"<div class=card><b>Executive summary</b><p>{html.escape(d['summary']['summary'])}</p></div>"]

    defects = _defects(d)
    H.append(f"<h2>Defects ({len(defects)})</h2>")
    for i, s in enumerate(defects, 1):
        reqs = ", ".join(sorted({a["covers"] for a in s["failed"]}))
        H.append(f"<div class=card id='{s['id']}'><div><span class='pill failed'>D{i} · {s['severity']}</span> <b>{html.escape(s['title'])}</b>"
                 f"<span class=kind>{s['kind']}</span><span class=kind>{s['id']}</span></div>"
                 f"<div class=meta>Requirements: {reqs}</div><details open><summary>Steps to reproduce</summary><ol>"
                 + "".join(f"<li>{html.escape(st)}</li>" for st in s["steps"]) + "</ol></details>")
        for a in s["failed"]:
            H.append(f"<div><b>{html.escape(a['statement'])}</b> <span class=meta>[{a['covers']}]</span></div>"
                     f"<div class=eo><div><small>Expected</small>{_esc(a['expected'])}</div>"
                     f"<div><small>Observed</small>{_esc(a['observed'])}</div></div>")
        fix = d["summary"].get("fixes", {}).get(s["id"])
        if fix:
            H.append(f"<div class=fix><b>Likely cause / fix:</b> {html.escape(fix)}</div>")
        shots = [e["value"] for e in s["evidence"] if e["kind"] == "screenshot"]
        if shots:
            H.append("<div class=shots>" + "".join(_img(out, p, True) for p in shots[:3]) + "</div>")
        other = [e for e in s["evidence"] if e["kind"] in ("value", "network")]
        if other:
            H.append("<details><summary>Observed values &amp; network evidence</summary>"
                     + "".join(f"<div class=meta>{html.escape(e['label'])}</div><pre>{html.escape(e['value'][:1500])}</pre>" for e in other)
                     + "</details>")
        H.append("</div>")

    H.append("<h2>Requirement coverage</h2><div class=card><table><tr><th>ID</th><th>Requirement</th><th>Verdict</th><th>Why</th></tr>")
    for r in d["requirements"]:
        ideas = ""
        if r["verdict"] == "unverified" and r.get("test_ideas"):
            ideas = "<br><span class=meta>Suggested: " + html.escape("; ".join(r["test_ideas"][:2])) + "</span>"
        H.append(f"<tr><td><b>{r['id']}</b></td><td>{html.escape(r['text'])}</td>"
                 f"<td><span class='pill {r['verdict']}'>{PILL[r['verdict']]}</span></td>"
                 f"<td>{html.escape(r['verdict_reason'])}{ideas}</td></tr>")
    H.append("</table></div>")

    H.append("<h2>All scenarios</h2><div class=card>")
    for s in d["scenarios"]:
        H.append(f"<details><summary><span class='pill {s['verdict']}'>{PILL[s['verdict']]}</span> {html.escape(s['title'])}"
                 f" <span class=kind>{s['kind']}</span><span class=kind>{s['id']}</span><span class=kind>{s['duration_s']}s</span></summary>"
                 f"<ol>" + "".join(f"<li>{html.escape(st)}</li>" for st in s["steps"]) + "</ol><table>"
                 + "".join(f"<tr><td><span class='pill {a['verdict']}'>{PILL[a['verdict']]}</span></td><td>{a['covers']}</td>"
                           f"<td>{html.escape(a['statement'])}</td><td class=meta>{_esc(a['observed'])}</td></tr>" for a in s["assertions"])
                 + "</table>" + (f"<p class=meta>Error: {html.escape(s['error'])}</p>" if s["error"] else "")
                 + "<div class=shots>" + "".join(_img(out, e["value"], False) for e in s["evidence"] if e["kind"] == "screenshot")
                 + "</div></details>")
    H.append("</div>")

    H.append("<h2>Cost &amp; efficiency</h2><div class=card><table><tr><th>Purpose</th><th>Model</th><th>Calls</th>"
             "<th>Cached</th><th>Input tok</th><th>Output tok</th><th>USD (provider)</th><th>USD (Claude-equiv.)</th></tr>")
    for r in _cost_rows(d):
        H.append(f"<tr><td>{html.escape(r['purpose'])}</td><td>{r['model']}</td><td>{r['calls']}</td><td>{r['cached']}</td>"
                 f"<td>{r['input_tokens']:,}</td><td>{r['output_tokens']:,}</td><td>${r['usd']:.4f}</td>"
                 f"<td>${r.get('claude_equiv_usd', r['usd']):.4f}</td></tr>")
    H.append(f"</table><p class=meta>Browser actions: {m['browser_actions']} · API responses captured: {m['api_events_captured']} · "
             f"Locator heals: {len(m['heals'])} · Deterministic fallbacks: {html.escape(', '.join(cost['fallbacks']) or 'none')}</p>")
    if m["compensations"]:
        H.append("<p class=meta><b>Test hygiene</b> – compensating transfers made to restore balances:<br>"
                 + "<br>".join(html.escape(x["action"]) for x in m["compensations"]) + "</p>")
    H.append("</div></div></body></html>")
    return "".join(H)
