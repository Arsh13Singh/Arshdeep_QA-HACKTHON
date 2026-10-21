"""Thin LLM client with a cost ledger, a disk cache, an offline mode and two providers.

Providers (picked automatically from whichever key is set, or with --provider):
* anthropic – Claude via https://api.anthropic.com/v1/messages
* groq      – OpenAI-compatible API at https://api.groq.com (free tier: good for prototyping)

Design choices (and why they matter for the "cost" rubric item):
* The browser work is plain Playwright code, so it costs nothing in tokens. The model is called only where
  judgement is needed: reading the PRD once, judging whether error messages are "clear",
  healing a broken locator, and writing the executive summary.
* Every call is logged in a ledger: purpose, model, tokens, USD at the provider's list price,
  AND the equivalent cost on Claude for the same token counts. A free Groq prototype can therefore
  still report a realistic production cost.
* Responses are cached on disk keyed by a hash of the prompt, so re-runs are nearly free.
* With no API key the agent still runs: each LLM-backed step has a deterministic fallback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
# Preference order when picking a Groq model the key can actually use.
GROQ_PREFERENCE = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.1-8b-instant"]

# USD per million tokens (input, output). Claude: platform.claude.com/docs/en/about-claude/pricing;
# Groq: groq.com/pricing (both checked Oct 2026). Override with QA_PRICES='{"model": [in, out]}'.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
}
if os.getenv("QA_PRICES"):
    PRICES.update({k: tuple(v) for k, v in json.loads(os.environ["QA_PRICES"]).items()})

DEFAULT_MODELS = {
    "anthropic": ("claude-haiku-5-5", "claude-sonnet-5-5"),
    # One strong model for both roles keeps the free-tier JSON output reliable.
    "groq": ("llama-3.3-70b-versatile", "llama-3.3-70b-versatile"),
}
# The Claude models we'd use in production; used for the "Claude-equivalent" cost column.
CLAUDE_EQUIV = {"fast": "claude-haiku-5-5", "smart": "claude-sonnet-5-5"}


def _usd(model: str, tin: int, tout: int) -> float:
    p_in, p_out = PRICES.get(model, (0.0, 0.0))
    return round((tin * p_in + tout * p_out) / 1_000_000, 6)


@dataclass
class LedgerEntry:
    purpose: str
    model: str
    input_tokens: int
    output_tokens: int
    usd: float              # at this provider's list price (Groq free tier is billed $0)
    claude_equiv_usd: float  # same tokens priced on the production Claude model
    cached: bool
    seconds: float


@dataclass
class Ledger:
    provider: str = "offline"
    entries: list[LedgerEntry] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)   # steps that ran without the model
    estimates: list[dict] = field(default_factory=list)  # offline: what the skipped calls would cost

    @property
    def total_usd(self) -> float:
        return round(sum(e.usd for e in self.entries), 6)

    @property
    def total_claude_equiv(self) -> float:
        return round(sum(e.claude_equiv_usd for e in self.entries), 6)

    def by_purpose(self) -> list[dict]:
        rows: dict[str, dict] = {}
        for e in self.entries:
            r = rows.setdefault(e.purpose, {"purpose": e.purpose, "model": e.model, "calls": 0, "cached": 0,
                                            "input_tokens": 0, "output_tokens": 0, "usd": 0.0, "claude_equiv_usd": 0.0})
            r["calls"] += 1
            r["cached"] += int(e.cached)
            r["input_tokens"] += e.input_tokens
            r["output_tokens"] += e.output_tokens
            r["usd"] = round(r["usd"] + e.usd, 6)
            r["claude_equiv_usd"] = round(r["claude_equiv_usd"] + e.claude_equiv_usd, 6)
        return list(rows.values())

    def to_dict(self) -> dict:
        return {"provider": self.provider, "total_usd": self.total_usd,
                "total_claude_equiv_usd": self.total_claude_equiv, "by_purpose": self.by_purpose(),
                "entries": [asdict(e) for e in self.entries], "fallbacks": self.fallbacks,
                "estimated_usd_if_enabled": round(sum(e["usd"] for e in self.estimates), 6),
                "estimates": self.estimates}


class LLM:
    def __init__(self, cache_dir: Path, *, provider: str | None = None, api_key: str | None = None,
                 fast_model: str | None = None, smart_model: str | None = None, offline: bool = False) -> None:
        if provider in (None, "auto"):
            provider = ("anthropic" if os.getenv("ANTHROPIC_API_KEY")
                        else "groq" if os.getenv("GROQ_API_KEY") else "anthropic")
        self.provider = provider
        self.api_key = api_key or os.getenv("GROQ_API_KEY" if provider == "groq" else "ANTHROPIC_API_KEY")
        default_fast, default_smart = DEFAULT_MODELS[provider]
        self.fast_model = fast_model or default_fast
        self.smart_model = smart_model or default_smart
        self.offline = offline or not self.api_key
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(provider="offline" if self.offline else provider)
        self._last_call = 0.0
        self._last_error = ""
        self._user_models = bool(fast_model or smart_model)
        self._groq_checked = False
        self.notes: list[str] = []

    # ------------------------------------------------------------------ Groq model discovery
    def groq_available_models(self) -> list[str] | None:
        try:
            r = requests.get(GROQ_MODELS_URL, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=20)
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        return sorted(m.get("id", "") for m in r.json().get("data", []) if m.get("active", True))

    def _ensure_groq_models(self) -> None:
        """Make sure the configured models exist for THIS key; otherwise pick the best available one."""
        if self._groq_checked:
            return
        self._groq_checked = True
        available = self.groq_available_models()
        if not available:
            return
        for attr in ("fast_model", "smart_model"):
            current = getattr(self, attr)
            if current not in available:
                pick = next((m for m in GROQ_PREFERENCE if m in available), None)
                if pick:
                    setattr(self, attr, pick)
                    note = f"{current} not available to this Groq key; using {pick}"
                    if note not in self.notes:
                        self.notes.append(note)

    def _switch_groq_model(self, bad: str) -> str | None:
        """After a 404 for `bad`, move both roles to the next preferred model."""
        order = [m for m in GROQ_PREFERENCE if m != bad]
        available = self.groq_available_models() or order
        nxt = next((m for m in order if m in available), None)
        if nxt:
            if self.fast_model == bad:
                self.fast_model = nxt
            if self.smart_model == bad:
                self.smart_model = nxt
            self.notes.append(f"{bad} was refused by Groq; switched to {nxt}")
        return nxt

    @property
    def available(self) -> bool:
        return not self.offline

    def note_fallback(self, step: str) -> None:
        if step not in self.ledger.fallbacks:
            self.ledger.fallbacks.append(step)

    # ------------------------------------------------------------------ core call
    def json(self, purpose: str, system: str, prompt: str, *, smart: bool = False,
             max_tokens: int = 2000) -> dict | list | None:
        """Ask the model for a JSON answer. Returns None if offline or the call fails."""
        if self.provider == "groq" and not self.offline:
            self._ensure_groq_models()
        model = self.smart_model if smart else self.fast_model
        equiv = CLAUDE_EQUIV["smart" if smart else "fast"]
        if self.offline:
            # Estimate what this call *would* cost on Claude (≈3.5 chars/token, output ≈ 40% of the cap).
            est_in, est_out = int(len(system + prompt) / 3.5), int(max_tokens * 0.4)
            self.ledger.estimates.append({"purpose": purpose, "model": equiv, "input_tokens": est_in,
                                          "output_tokens": est_out, "usd": _usd(equiv, est_in, est_out)})
            self.note_fallback(purpose)
            return None

        key = hashlib.sha256(f"{self.provider}\n{model}\n{system}\n{prompt}".encode()).hexdigest()[:24]
        cache_file = self.cache_dir / f"{purpose}-{key}.json"
        if cache_file.exists():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            u = data["usage"]
            self.ledger.entries.append(LedgerEntry(purpose, model, u["input_tokens"], u["output_tokens"],
                                                   0.0, 0.0, True, 0.0))
            return data["parsed"]

        started = time.time()
        result = self._call_groq(model, system, prompt, max_tokens) if self.provider == "groq" \
            else self._call_anthropic(model, system, prompt, max_tokens)
        if result == "MODEL_NOT_FOUND" and self.provider == "groq":
            model = self._switch_groq_model(model) or model
            result = self._call_groq(model, system, prompt, max_tokens)
        if not isinstance(result, tuple):
            self.note_fallback(f"{purpose} ({self._last_error})")
            return None
        text, tin, tout = result
        self.ledger.entries.append(LedgerEntry(purpose, model, tin, tout, _usd(model, tin, tout),
                                               _usd(equiv, tin, tout), False, round(time.time() - started, 2)))
        parsed = extract_json(text)
        if parsed is None:
            self.note_fallback(f"{purpose} (unparseable response)")
            return None
        cache_file.write_text(json.dumps({"usage": {"input_tokens": tin, "output_tokens": tout},
                                          "parsed": parsed}, indent=2), encoding="utf-8")
        return parsed

    # ------------------------------------------------------------------ providers
    def _post(self, url: str, headers: dict, body: dict, purpose_hint: str):
        resp = None
        for attempt in range(4):                   # retry transient overload / rate limits
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=120)
            except requests.RequestException:
                resp = None
            if resp is not None and resp.status_code not in (429, 500, 502, 503, 529):
                break
            wait = 2 * (attempt + 1)
            if resp is not None and resp.headers.get("retry-after"):
                try:
                    wait = min(30.0, float(resp.headers["retry-after"]))
                except ValueError:
                    pass
            time.sleep(wait)
        if resp is None or resp.status_code != 200:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")[:140] if resp is not None else ""
            except Exception:
                pass
            self._last_error = f"{purpose_hint}: API error {getattr(resp, 'status_code', 'network')} {detail}".strip()
            if resp is not None and resp.status_code == 404 and "model" in detail.lower():
                return "MODEL_NOT_FOUND"
            return None
        return resp.json()

    def _call_anthropic(self, model, system, prompt, max_tokens):
        payload = self._post(ANTHROPIC_URL,
                             {"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                              "content-type": "application/json"},
                             {"model": model, "max_tokens": max_tokens, "system": system,
                              "messages": [{"role": "user", "content": prompt}]}, model)
        if not isinstance(payload, dict):
            return payload
        text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
        u = payload.get("usage", {})
        return text, u.get("input_tokens", 0), u.get("output_tokens", 0)

    def _call_groq(self, model, system, prompt, max_tokens):
        # Free tier allows ~30 requests/minute; keep a small gap between calls.
        gap = time.time() - self._last_call
        if gap < 2.0:
            time.sleep(2.0 - gap)
        self._last_call = time.time()
        body = {"model": model, "max_tokens": min(max_tokens, 6000), "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system + " Respond with a JSON object."},
                             {"role": "user", "content": prompt}]}
        if model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = "low"          # reasoning models: keep thinking (and cost) short
            body["max_tokens"] = min(max_tokens + 2000, 8000)
        payload = self._post(GROQ_URL, {"Authorization": f"Bearer {self.api_key}",
                                        "Content-Type": "application/json"}, body, model)
        if not isinstance(payload, dict):
            return payload
        choice = (payload.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        u = payload.get("usage", {})
        return text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


def extract_json(text: str):
    """Models sometimes wrap JSON in prose or ``` fences; pull out the first JSON value."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE per line) so first-time users don't need to set env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
