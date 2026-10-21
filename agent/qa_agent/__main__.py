"""CLI entry point:  python -m qa_agent --url http://localhost:5173 --prd ../test-website/PRD.md"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .llm import load_dotenv
from .runner import run


def check_llm(provider: str, model: str | None) -> int:
    import tempfile
    from .llm import LLM
    llm = LLM(Path(tempfile.mkdtemp()), provider=provider, fast_model=model)
    if llm.offline:
        print("No API key found. Put GROQ_API_KEY=... (or ANTHROPIC_API_KEY=...) in agent/.env")
        return 1
    print(f"Provider: {llm.provider}")
    if llm.provider == "groq":
        models = llm.groq_available_models()
        if models is None:
            print("Could not list models: the key is probably invalid (check agent/.env for typos/quotes).")
            return 1
        print("Models this key can use:", ", ".join(models))
    answer = llm.json("check", "You are a test.", 'Reply with the JSON object {"ok": true}.', max_tokens=50)
    for note in llm.notes:
        print("Note:", note)
    if isinstance(answer, dict) and answer.get("ok"):
        e = llm.ledger.entries[-1]
        print(f"OK: {e.model} answered ({e.input_tokens} in / {e.output_tokens} out tokens, "
              f"list price ${e.usd:.6f}, Claude-equivalent ${e.claude_equiv_usd:.6f})")
        return 0
    print("FAILED:", "; ".join(llm.ledger.fallbacks) or "no usable answer")
    return 1


def main() -> int:
    # Windows consoles default to a legacy code page; force UTF-8 so ✔/✘/→ print safely.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # API keys can live in agent/.env (GROQ_API_KEY=... or ANTHROPIC_API_KEY=...).
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv(Path.cwd() / ".env")

    p = argparse.ArgumentParser(prog="qa_agent", description="Specification-driven QA agent for DemoBank")
    p.add_argument("--url", help="Frontend URL of the app under test")
    p.add_argument("--prd", type=Path, help="Path to PRD.md")
    p.add_argument("--check-llm", action="store_true",
                   help="Only test the API key: list usable models and make one tiny call, then exit")
    p.add_argument("--out", type=Path, default=Path("runs"), help="Output directory (default: runs/)")
    p.add_argument("--api-url", help="Backend base URL (auto-discovered from network traffic if omitted)")
    p.add_argument("--provider", choices=["auto", "groq", "anthropic"], default="auto",
                   help="LLM provider (auto = whichever API key is set)")
    p.add_argument("--offline", action="store_true", help="Never call an LLM; use deterministic fallbacks")
    p.add_argument("--headed", action="store_true", help="Show the browser window (good for the demo video)")
    p.add_argument("--only", nargs="*", help="Run only these suites or scenario-ID prefixes, e.g. --only transfers HIS-")
    p.add_argument("--chaos", action="store_true",
                   help="Rename form labels/buttons at runtime to demonstrate locator self-healing")
    p.add_argument("--fast-model", help="Override the cheap model (judge, healing, summary)")
    p.add_argument("--smart-model", help="Override the PRD-analysis model")
    a = p.parse_args()
    if a.check_llm:
        return check_llm(a.provider, a.fast_model)
    if not a.url or not a.prd:
        p.error("--url and --prd are required (or use --check-llm)")
    if not a.prd.exists():
        print(f"PRD not found: {a.prd}", file=sys.stderr)
        return 2
    run(url=a.url, prd=a.prd, out_root=a.out, offline=a.offline, headed=a.headed, only=a.only,
        api_url=a.api_url, chaos=a.chaos, fast_model=a.fast_model, smart_model=a.smart_model,
        provider=a.provider)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
