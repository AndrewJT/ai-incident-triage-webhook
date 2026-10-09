"""Orchestrates retrieval -> inference (with fallback) -> dispatch for one event."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .dispatch import Dispatcher
from .fallback import deterministic_triage
from .knowledge import KnowledgeBase
from .llm import TriageClient, TriageError
from .models import NormalizedEvent
from .prompt import pack_context

log = logging.getLogger("triage.pipeline")


@dataclass
class Job:
    event_id: str
    event: NormalizedEvent
    enqueued_at: float


class Pipeline:
    def __init__(self, settings: Settings, kb: KnowledgeBase, llm: TriageClient, dispatcher: Dispatcher) -> None:
        self._s, self._kb, self._llm, self._dispatcher = settings, kb, llm, dispatcher

    async def run(self, job: Job) -> dict[str, Any]:
        event = job.event
        t0 = time.perf_counter()
        queue_wait_ms = (t0 - job.enqueued_at) * 1000

        # Context retrieval: query = type + message + head of trace (not the whole trace)
        query = f"{event.event_type} {event.message} {event.trace[:600]}"
        hits = await self._kb.retrieve(query, self._s.top_k, self._s.min_score)
        context = pack_context(hits, self._s.max_context_chars)
        t1 = time.perf_counter()

        # Structured inference, deterministic fallback on any failure
        try:
            triage, source = await self._llm.triage(event, context), "llm"
        except TriageError as exc:
            log.warning("LLM triage failed for %s, using fallback: %s", job.event_id, exc)
            triage, source = deterministic_triage(event, hits, self._kb), "fallback"
        t2 = time.perf_counter()

        report = await self._dispatcher.dispatch(job.event_id, event, triage, source)
        t3 = time.perf_counter()

        return {
            "event_id": job.event_id,
            "event": event.model_dump(mode="json"),
            "triage": triage.model_dump(mode="json"),
            "source": source,
            "retrieved": [
                {"title": h.chunk.title, "section": h.chunk.section, "kind": h.chunk.kind, "score": round(h.score, 3)}
                for h in hits
            ],
            "context_chars": len(context),
            "dispatch": report,
            "timings_ms": {
                "queue_wait": round(queue_wait_ms, 1),
                "retrieval": round((t1 - t0) * 1000, 1),
                "inference": round((t2 - t1) * 1000, 1),
                "dispatch": round((t3 - t2) * 1000, 1),
                "total": round(queue_wait_ms + (t3 - t0) * 1000, 1),
            },
        }
