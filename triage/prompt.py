"""Prompt construction with a hard budget (the latency optimisation lever)."""
from __future__ import annotations

from .knowledge import Hit
from .models import NormalizedEvent

SYSTEM_PROMPT = """You are an SRE incident-triage assistant.
Analyse the incident event and the retrieved runbook context, then reply with
exactly ONE JSON object and nothing else - no markdown fences, no commentary.

Required keys (and only these keys):
- "severity": one of "critical", "high", "medium", "low"
- "failure_mode": short name of the most likely failure mode
- "root_cause_probability": number from 0 to 1, your confidence that failure_mode is the root cause
- "summary": one or two sentences describing what happened
- "remediation_steps": 1 to 8 short imperative steps, most urgent first

Rules: prefer steps from the provided runbooks when they apply; never invent
runbooks or commands you are unsure about; if the context is irrelevant, say so
in "summary" and lower "root_cause_probability"."""


def trim_trace(trace: str, max_lines: int, max_chars: int) -> str:
    """Keep the head (exception line) and tail (innermost frames / cause) of a trace."""
    lines = trace.splitlines()
    if len(lines) > max_lines:
        head = max(1, max_lines // 6)
        tail = max_lines - head
        omitted = len(lines) - head - tail
        lines = lines[:head] + [f"... {omitted} lines omitted ..."] + lines[-tail:]
    text = "\n".join(lines)
    if len(text) > max_chars:
        half = max_chars // 2
        text = text[:half] + "\n... truncated ...\n" + text[-half:]
    return text


def pack_context(hits: list[Hit], max_chars: int) -> str:
    """Pack retrieved chunks, best first, until the character budget is spent."""
    parts: list[str] = []
    remaining = max_chars
    for hit in hits:
        header = f"[{hit.chunk.kind}: {hit.chunk.title} / {hit.chunk.section}]\n"
        room = remaining - len(header)
        if room < 80:
            break
        body = hit.chunk.text if len(hit.chunk.text) <= room else hit.chunk.text[:room].rstrip() + " ..."
        parts.append(header + body)
        remaining -= len(header) + len(body) + 2
    return "\n\n".join(parts)


def build_user_prompt(event: NormalizedEvent, context: str, max_trace_lines: int, max_trace_chars: int) -> str:
    trace = trim_trace(event.trace, max_trace_lines, max_trace_chars) if event.trace else "(none)"
    return (
        f"## Event\nservice: {event.service}\ntype: {event.event_type}\nlevel: {event.level}\n"
        f"time: {event.timestamp.isoformat()}\nmessage: {event.message}\n\n"
        f"## Error trace\n{trace}\n\n"
        f"## Retrieved context\n{context or '(no relevant runbooks found)'}\n"
    )
