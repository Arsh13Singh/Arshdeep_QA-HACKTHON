"""Unit tests for the parts that don't need a browser. Run: python -m pytest -q"""
from pathlib import Path

import pytest

from qa_agent import llm as llm_mod
from qa_agent.judge import heuristic_clear
from qa_agent.llm import LLM, extract_json
from qa_agent.models import Assertion, Requirement, Scenario, roll_up
from qa_agent.prd import parse_prd, resolve_anchor
from qa_agent.report import severity

PRD = Path(__file__).resolve().parents[2] / "test-website" / "PRD.md"


def test_prd_parser_ids_are_stable_and_complete():
    reqs = parse_prd(PRD.read_text())
    ids = [r.id for r in reqs]
    assert len(ids) == len(set(ids))
    assert {"SES-01", "TRF-04", "HIS-04", "UI-05", "SEED-02", "GEN-01"} <= set(ids)
    assert not any("Out of scope" in r.section for r in reqs)
    assert parse_prd(PRD.read_text())[10].id == reqs[10].id      # deterministic


def test_anchor_resolution():
    reqs = parse_prd(PRD.read_text())
    assert resolve_anchor("start and end dates are inclusive", reqs) == "HIS-04"
    assert resolve_anchor("If funds are   insufficient", reqs) == "TRF-05"
    assert resolve_anchor("this phrase is not in the PRD", reqs) is None


def test_roll_up_fail_beats_pass_and_missing_is_unverified():
    reqs = [Requirement("A-01", "s", "a"), Requirement("A-02", "s", "b"), Requirement("A-03", "s", "c")]
    sc = Scenario("X", "x", "positive", [])
    sc.assertions = [Assertion("A-01", "ok", "passed"), Assertion("A-01", "bad", "failed"),
                     Assertion("A-02", "ok", "passed")]
    roll_up(reqs, [sc])
    assert [r.verdict for r in reqs] == ["failed", "passed", "unverified"]


def test_extract_json_handles_fences_and_prose():
    assert extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('result: [1, 2] done') == [1, 2]
    assert extract_json("no json") is None


def test_clarity_heuristic():
    assert heuristic_clear("Source and destination accounts must be different.")[0]
    assert not heuristic_clear("The request is invalid.")[0]
    assert not heuristic_clear("")[0]


def test_severity_rules():
    assert severity([{"statement": "Overdraft: balances are unchanged", "covers": "TRF-05"}]) == "Critical"
    assert severity([{"statement": "start = end includes X", "covers": "HIS-04"}]) == "Medium"


class FakeResp:
    status_code = 200

    def json(self):
        return {"content": [{"type": "text", "text": '{"items": []}'}],
                "usage": {"input_tokens": 1_000_000, "output_tokens": 100_000}}


def test_llm_cost_ledger_and_cache(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(llm_mod.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
    client = LLM(tmp_path, api_key="test-key")
    assert client.json("clarity_judge", "sys", "prompt") == {"items": []}
    # Haiku 5.5: 1M input × $0.10 + 0.1M output × $0.50 = $0.15
    assert client.ledger.total_usd == pytest.approx(0.15)
    assert client.json("clarity_judge", "sys", "prompt") == {"items": []}   # served from disk cache
    assert len(calls) == 1 and client.ledger.entries[-1].cached and client.ledger.total_usd == pytest.approx(0.15)


def test_offline_mode_estimates_cost(tmp_path):
    client = LLM(tmp_path, offline=True)
    assert client.json("prd_enrichment", "s" * 350, "p" * 3150, smart=True, max_tokens=1000) is None
    est = client.ledger.to_dict()["estimates"][0]
    assert est["input_tokens"] == 1000 and est["output_tokens"] == 400
    assert "prd_enrichment" in client.ledger.fallbacks


class FakeGroqResp:
    status_code = 200
    headers: dict = {}

    def json(self):
        return {"choices": [{"message": {"content": '{"items": [{"i": 0, "clear": true}]}'}}],
                "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 100_000}}


class FakeModels:
    status_code = 200

    def __init__(self, ids):
        self.ids = ids

    def json(self):
        return {"data": [{"id": i, "active": True} for i in self.ids]}


def test_groq_picks_an_available_model(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_mod.requests, "get", lambda url, **k: FakeModels(["openai/gpt-oss-20b", "llama-3.1-8b-instant"]))
    monkeypatch.setattr(llm_mod.requests, "post", lambda url, **k: FakeGroqResp())
    client = LLM(tmp_path, provider="groq", api_key="gsk-test")
    client._last_call = 0
    client.json("clarity_judge", "sys", "prompt")
    assert client.fast_model == "openai/gpt-oss-20b" and client.notes


def test_groq_provider_and_claude_equivalent_cost(tmp_path, monkeypatch):
    sent = {}
    monkeypatch.setattr(llm_mod.requests, "post", lambda url, **k: sent.update(url=url, body=k["json"]) or FakeGroqResp())
    monkeypatch.setattr(llm_mod.requests, "get", lambda url, **k: FakeModels(["llama-3.3-70b-versatile"]))
    client = LLM(tmp_path, provider="groq", api_key="gsk-test")
    client._last_call = 0
    assert client.json("clarity_judge", "sys", "prompt")["items"][0]["clear"] is True
    assert "groq.com" in sent["url"] and sent["body"]["model"] == "llama-3.3-70b-versatile"
    e = client.ledger.entries[0]
    assert e.usd == pytest.approx(0.59 + 0.079)                 # Groq list price for 1M in / 0.1M out
    assert e.claude_equiv_usd == pytest.approx(0.10 + 0.05)     # same tokens on Claude Haiku 5.5
