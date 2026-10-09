"""Queue buffering, worker pool, dedup and metrics.

The webhook handler only validates and enqueues (fast 202). A bounded worker
pool drains the queue, so a burst of events cannot overload the local LLM:
excess load waits in the buffer (or is shed with 429 once the buffer is full).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from collections import OrderedDict, deque
from typing import Any

from .models import NormalizedEvent
from .pipeline import Job, Pipeline

log = logging.getLogger("triage.queue")


class QueueFull(Exception):
    pass


class Metrics:
    def __init__(self) -> None:
        self.counts = {"accepted": 0, "deduplicated": 0, "rejected_full": 0, "invalid": 0,
                       "processed": 0, "failed": 0, "llm": 0, "fallback": 0}
        self._latency: deque[float] = deque(maxlen=1000)

    def observe(self, total_ms: float) -> None:
        self._latency.append(total_ms)

    @staticmethod
    def _pct(values: list[float], p: float) -> float | None:
        return round(values[min(len(values) - 1, int(p * len(values)))], 1) if values else None

    def snapshot(self, queue_depth: int) -> dict[str, Any]:
        ordered = sorted(self._latency)
        return {**self.counts, "queue_depth": queue_depth,
                "latency_ms": {"p50": self._pct(ordered, 0.50), "p95": self._pct(ordered, 0.95)}}


def fingerprint(event: NormalizedEvent) -> str:
    stable = re.sub(r"\d+", "#", event.message.lower())
    return hashlib.sha1(f"{event.service}|{event.event_type}|{stable}".encode()).hexdigest()


class TriageQueue:
    def __init__(self, pipeline: Pipeline, metrics: Metrics, size: int, workers: int,
                 dedup_ttl_s: float, result_cap: int = 1000) -> None:
        self._pipeline, self.metrics = pipeline, metrics
        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=size)
        self._n_workers = workers
        self._tasks: list[asyncio.Task] = []
        self._dedup_ttl = dedup_ttl_s
        self._seen: dict[str, tuple[float, str]] = {}
        self._results: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._result_cap = result_cap

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._worker(i)) for i in range(self._n_workers)]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _set(self, event_id: str, value: dict[str, Any]) -> None:
        self._results[event_id] = value
        self._results.move_to_end(event_id)
        while len(self._results) > self._result_cap:
            self._results.popitem(last=False)

    def get(self, event_id: str) -> dict[str, Any] | None:
        return self._results.get(event_id)

    def submit(self, event: NormalizedEvent) -> tuple[str, str]:
        """Returns (event_id, status). Raises QueueFull when the buffer is saturated."""
        now = time.monotonic()
        key = fingerprint(event)
        self._seen = {k: v for k, v in self._seen.items() if now - v[0] < self._dedup_ttl}
        if key in self._seen:
            self.metrics.counts["deduplicated"] += 1
            return self._seen[key][1], "duplicate"

        event_id = uuid.uuid4().hex[:12]
        try:
            self._queue.put_nowait(Job(event_id, event, time.perf_counter()))
        except asyncio.QueueFull:
            self.metrics.counts["rejected_full"] += 1
            raise QueueFull from None
        self._seen[key] = (now, event_id)
        self._set(event_id, {"event_id": event_id, "status": "queued"})
        self.metrics.counts["accepted"] += 1
        return event_id, "queued"

    async def _worker(self, n: int) -> None:
        while True:
            job = await self._queue.get()
            try:
                self._set(job.event_id, {"event_id": job.event_id, "status": "processing"})
                result = await self._pipeline.run(job)
                self._set(job.event_id, {"status": "done", **result})
                self.metrics.counts["processed"] += 1
                self.metrics.counts[result["source"]] += 1
                self.metrics.observe(result["timings_ms"]["total"])
            except Exception as exc:  # noqa: BLE001
                log.exception("worker %d failed on %s", n, job.event_id)
                self._set(job.event_id, {"event_id": job.event_id, "status": "failed", "error": str(exc)})
                self.metrics.counts["failed"] += 1
            finally:
                self._queue.task_done()
