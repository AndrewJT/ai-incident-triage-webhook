import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from triage.config import ROOT, Settings
from triage.fallback import deterministic_triage
from triage.knowledge import HashingEmbedder, KnowledgeBase
from triage.llm import OllamaTriageClient, TriageError, extract_json
from triage.main import create_app
from triage.models import Triage
from triage.normalize import InvalidEvent, normalize
from triage.prompt import pack_context, trim_trace

GOOD = {
    "severity": "high",
    "failure_mode": "Connection pool exhaustion",
    "root_cause_probability": 0.8,
    "summary": "Pool exhausted.",
    "remediation_steps": ["Restart workers", "Raise pool size"],
}


# ---------------------------------------------------------------- normalize
def test_normalize_json_aliases_and_epoch():
    ev = normalize({"source": "api", "severity": "FATAL", "ts": 1791533700, "msg": "boom",
                    "error": "java.lang.OutOfMemoryError: Java heap space"})
    assert ev.service == "api" and ev.level == "critical"
    assert ev.event_type == "java.lang.OutOfMemoryError"
    assert ev.timestamp.year == 2026 and ev.timestamp.tzinfo is not None


def test_normalize_raw_text_extracts_timestamp_level_type():
    ev = normalize("2026-10-09 08:00:01,123 ERROR payments Connection refused\n  at foo()")
    assert ev.level == "error" and ev.event_type == "ConnectionRefused"
    assert ev.timestamp.hour == 8 and ev.trace


@pytest.mark.parametrize("bad", ["", "   ", {}, {"level": "error"}, [1, 2], 42])
def test_normalize_rejects_garbage(bad):
    with pytest.raises(InvalidEvent):
        normalize(bad)


# ---------------------------------------------------------------- retrieval
@pytest.fixture(scope="module")
def kb():
    return asyncio.run(KnowledgeBase.build(ROOT / "knowledge", HashingEmbedder()))


def test_retrieval_matches_right_runbook(kb):
    hits = asyncio.run(kb.retrieve("OutOfMemoryError Java heap space container killed", 3, 0.1))
    assert hits and hits[0].chunk.title.startswith("Out-of-memory")
    hits = asyncio.run(kb.retrieve("too many connections pool exhausted OperationalError", 3, 0.1))
    assert "connection pool" in hits[0].chunk.title.lower() or hits[0].chunk.kind == "resolution"


def test_context_budget_and_trace_trimming(kb):
    hits = asyncio.run(kb.retrieve("timeout connection refused upstream", 5, 0.0))
    assert len(pack_context(hits, 400)) <= 400
    trace = "\n".join(f"frame {i}" for i in range(500))
    trimmed = trim_trace(trace, 30, 2500)
    assert len(trimmed.splitlines()) == 31 and "omitted" in trimmed and "frame 499" in trimmed


# ---------------------------------------------------------------- llm client
def _client(responses, **overrides):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        content = responses[min(len(calls) - 1, len(responses) - 1)]
        return httpx.Response(200, json={"message": {"content": content}})

    s = Settings(**overrides)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaTriageClient(s, http), calls


EVENT = normalize({"service": "x", "level": "error", "message": "boom"})


def test_extract_json_handles_markdown_wrapping():
    assert extract_json("```json\n" + json.dumps(GOOD) + "\n```")["severity"] == "high"
    assert extract_json("Sure! here you go: " + json.dumps(GOOD) + " hope it helps")["severity"] == "high"


def test_llm_sends_schema_and_accepts_valid_output():
    client, calls = _client([json.dumps(GOOD)])
    triage = asyncio.run(client.triage(EVENT, "ctx"))
    assert isinstance(triage, Triage) and calls[0]["format"]["additionalProperties"] is False
    assert calls[0]["options"]["temperature"] == 0


def test_llm_repairs_after_hallucinated_key():
    bad = json.dumps({**GOOD, "confidence_emoji": "🔥"})
    client, calls = _client([bad, json.dumps(GOOD)])
    assert asyncio.run(client.triage(EVENT, "")).severity == "high"
    assert len(calls) == 2 and "rejected" in calls[1]["messages"][-1]["content"]


def test_llm_raises_after_retries_exhausted():
    client, calls = _client(["not json at all"], llm_max_retries=1)
    with pytest.raises(TriageError):
        asyncio.run(client.triage(EVENT, ""))
    assert len(calls) == 2


def test_llm_unreachable_fails_fast_without_retry():
    attempts = []

    def handler(request):
        attempts.append(1)
        raise httpx.ConnectError("refused", request=request)

    client = OllamaTriageClient(Settings(llm_max_retries=3), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(TriageError):
        asyncio.run(client.triage(EVENT, ""))
    assert len(attempts) == 1


# ---------------------------------------------------------------- fallback
def test_fallback_uses_runbook_remediation(kb):
    ev = normalize({"service": "inv", "level": "critical",
                    "message": "OutOfMemoryError: Java heap space"})
    hits = asyncio.run(kb.retrieve("OutOfMemoryError Java heap space", 3, 0.1))
    triage = deterministic_triage(ev, hits, kb)
    assert triage.severity == "critical" and "Restart" in triage.remediation_steps[0]


# ---------------------------------------------------------------- end to end
class BrokenLLM:
    async def triage(self, event, context):
        raise TriageError("simulated invalid output")


class GoodLLM:
    async def triage(self, event, context):
        return Triage(**GOOD)


def _app(tmp_path: Path, llm, **kw):
    s = Settings(outbox_path=tmp_path / "outbox.jsonl", dedup_ttl_s=60, **kw)
    return create_app(s, llm=llm)


def _wait(client, event_id):
    for _ in range(100):
        data = client.get(f"/events/{event_id}").json()
        if data["status"] in ("done", "failed"):
            return data
        time.sleep(0.05)
    raise AssertionError("timeout")


def test_e2e_llm_path_creates_ticket_and_alert(tmp_path):
    with TestClient(_app(tmp_path, GoodLLM())) as client:
        r = client.post("/webhook/telemetry", json={"service": "orders", "level": "error",
                                                    "message": "too many connections"})
        assert r.status_code == 202
        data = _wait(client, r.json()["event_id"])
        assert data["status"] == "done" and data["source"] == "llm"
        assert data["dispatch"]["ticket"]["id"].startswith("LOCAL-")
        assert client.get("/metrics").json()["processed"] == 1
    channels = [json.loads(line)["channel"] for line in (tmp_path / "outbox.jsonl").read_text().splitlines()]
    assert channels == ["ticket", "chat"]


def test_e2e_invalid_llm_output_falls_back(tmp_path):
    with TestClient(_app(tmp_path, BrokenLLM())) as client:
        r = client.post("/webhook/telemetry", content="ERROR db Out of memory: Killed process 42",
                        headers={"content-type": "text/plain"})
        data = _wait(client, r.json()["event_id"])
        assert data["source"] == "fallback" and data["triage"]["severity"] == "critical"


def test_validation_signature_dedup_and_backpressure(tmp_path):
    with TestClient(_app(tmp_path, GoodLLM(), webhook_secret="s3cret")) as client:
        assert client.post("/webhook/telemetry", json={"message": "x"}).status_code == 401

    with TestClient(_app(tmp_path, GoodLLM())) as client:
        assert client.post("/webhook/telemetry", json={"level": "error"}).status_code == 422
        a = client.post("/webhook/telemetry", json={"service": "a", "message": "error 500 on shard 1"}).json()
        b = client.post("/webhook/telemetry", json={"service": "a", "message": "error 500 on shard 2"}).json()
        assert b["status"] == "duplicate" and a["event_id"] == b["event_id"]
        big = client.post("/webhook/telemetry", content=b"x" * 70_000)
        assert big.status_code == 413
