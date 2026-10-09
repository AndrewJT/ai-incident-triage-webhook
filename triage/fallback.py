"""Deterministic fallback triage: used whenever the LLM is unreachable or its
output fails validation, so every event still produces a valid `Triage`."""
from __future__ import annotations

import re

from .knowledge import Hit, KnowledgeBase
from .models import NormalizedEvent, Triage

_CRITICAL = re.compile(
    r"out of memory|\boom|heap space|no space left|disk (?:is )?full|data loss|corrupt|panic|"
    r"all replicas|certificate (?:has )?expired", re.I)
_HIGH = re.compile(
    r"connection refused|econnrefused|too many connections|pool exhausted|timed? ?out|deadline exceeded|"
    r"deadlock|unavailable|\b5\d\d\b|lag", re.I)

_GENERIC_STEPS = [
    "Check recent deploys and configuration changes for the affected service.",
    "Inspect service logs and dashboards around the event timestamp.",
    "Escalate to the on-call engineer for the owning team if impact persists.",
]


def _severity(event: NormalizedEvent) -> str:
    text = f"{event.message}\n{event.trace}"
    if event.level == "critical" or _CRITICAL.search(text):
        return "critical"
    if event.level == "error":
        return "high" if _HIGH.search(text) else "medium"
    if event.level == "warning":
        return "medium" if _HIGH.search(text) else "low"
    return "low"


def deterministic_triage(event: NormalizedEvent, hits: list[Hit], kb: KnowledgeBase) -> Triage:
    best = hits[0] if hits else None
    steps: list[str] = []
    if best:
        if best.chunk.kind == "runbook":
            steps = kb.remediation_for(best.chunk.title)
        else:
            resolution = best.chunk.text.split("Resolution:", 1)[-1].strip()
            steps = [resolution]
    steps = (steps or _GENERIC_STEPS)[:8]

    probability = round(min(0.85, 0.3 + 0.6 * best.score), 2) if best else 0.15
    failure_mode = best.chunk.title if best else event.event_type
    source = f"matched runbook '{best.chunk.title}'" if best else "no matching runbook"
    return Triage(
        severity=_severity(event),  # type: ignore[arg-type]
        failure_mode=failure_mode[:120],
        root_cause_probability=probability,
        summary=f"[rule-based] {event.event_type} in {event.service}: {event.message[:300]} ({source}).",
        remediation_steps=steps,
    )
