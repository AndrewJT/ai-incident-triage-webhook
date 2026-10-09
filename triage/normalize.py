"""Stage 1 - validation & extraction.

Accepts messy telemetry (JSON with varying field names, or raw log text) and
produces a `NormalizedEvent` with event type, timestamp and error trace.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .models import NormalizedEvent

MAX_MESSAGE_CHARS = 2000
MAX_TRACE_CHARS = 20000


class InvalidEvent(ValueError):
    """Payload cannot be turned into a usable event."""


ALIASES = {
    "message": ("message", "msg", "log", "text", "description", "summary", "body"),
    "trace": ("stack_trace", "stacktrace", "trace", "traceback", "exception", "error"),
    "timestamp": ("timestamp", "time", "ts", "@timestamp", "occurred_at", "date"),
    "level": ("level", "severity", "priority", "log_level", "loglevel"),
    "service": ("service", "source", "app", "application", "component", "host"),
    "type": ("event_type", "type", "alert_name", "alertname", "name"),
}

_LEVELS = {
    "debug": "debug", "trace": "debug",
    "info": "info", "information": "info", "notice": "info",
    "warn": "warning", "warning": "warning",
    "error": "error", "err": "error", "severe": "error",
    "critical": "critical", "crit": "critical", "fatal": "critical",
    "emergency": "critical", "alert": "critical", "p1": "critical", "p2": "error",
}

_TS_IN_TEXT = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_LEVEL_IN_TEXT = re.compile(r"\b(FATAL|CRITICAL|ERROR|SEVERE|WARN(?:ING)?|INFO|DEBUG)\b")
_EXC_CLASS = re.compile(r"\b((?:[A-Za-z_]\w*\.)*[A-Z][A-Za-z0-9_]*(?:Error|Exception))\b")
_HTTP_5XX = re.compile(r"\b(?:HTTP\s*)?(5\d\d)\b")

# (regex, event_type) used when no explicit type / exception class is present
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"out of memory|\boom\b|oomkilled|heap space", re.I), "OutOfMemory"),
    (re.compile(r"no space left|disk (?:is )?full|disk usage", re.I), "DiskFull"),
    (re.compile(r"connection refused|econnrefused", re.I), "ConnectionRefused"),
    (re.compile(r"too many connections|pool exhausted|connection pool", re.I), "ConnectionPoolExhausted"),
    (re.compile(r"timed? ?out|deadline exceeded", re.I), "Timeout"),
    (re.compile(r"certificate|x509|ssl handshake", re.I), "TlsCertificateError"),
    (re.compile(r"consumer lag|lag exceeded", re.I), "ConsumerLag"),
    (re.compile(r"deadlock", re.I), "Deadlock"),
]


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("stack", "stacktrace", "stack_trace", "trace", "traceback"):
            if isinstance(value.get(key), str) and value[key].strip():
                head = _as_text(value.get("message") or value.get("type") or "")
                return f"{head}\n{value[key].strip()}".strip()
        return json.dumps(value, default=str)
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(v) for v in value).strip()
    return str(value)


def _pick(payload: dict, field: str) -> Any:
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for alias in ALIASES[field]:
        value = lowered.get(alias)
        if value not in (None, "", [], {}):
            return value
    return None


def parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d+(\.\d+)?", value.strip())):
            seconds = float(value)
            if seconds > 1e11:  # milliseconds
                seconds /= 1000
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        text = str(value).strip().replace(",", ".")
        if " " in text and "T" not in text:
            text = text.replace(" ", "T", 1)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def normalize_level(value: Any) -> str | None:
    if value is None:
        return None
    return _LEVELS.get(str(value).strip().lower())


def infer_event_type(explicit: str, message: str, trace: str) -> str:
    if explicit:
        return explicit[:80]
    text = f"{message}\n{trace}"
    match = _EXC_CLASS.search(text)
    if match:
        return match.group(1)
    for pattern, name in _PATTERNS:
        if pattern.search(text):
            return name
    match = _HTTP_5XX.search(text)
    if match:
        return f"Http{match.group(1)}"
    return "unknown"


def normalize(payload: Any) -> NormalizedEvent:
    """Validate and normalise a decoded webhook body (dict) or raw text (str)."""
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            raise InvalidEvent("empty payload")
        first_line = text.splitlines()[0]
        payload = {"message": first_line, "trace": text if "\n" in text else ""}
    if not isinstance(payload, dict):
        raise InvalidEvent("payload must be a JSON object or plain text")

    trace = _as_text(_pick(payload, "trace"))
    message = _as_text(_pick(payload, "message"))
    if not message and trace:
        message = trace.splitlines()[0]
    if not message:
        raise InvalidEvent("payload has neither a message nor an error trace")
    if message == trace:
        trace = ""  # don't send the same text twice

    combined = f"{message}\n{trace}"
    timestamp = (
        parse_timestamp(_pick(payload, "timestamp"))
        or parse_timestamp((_TS_IN_TEXT.search(combined) or [None])[0])
        or datetime.now(timezone.utc)
    )

    level = normalize_level(_pick(payload, "level"))
    if level is None:
        found = _LEVEL_IN_TEXT.search(combined)
        level = normalize_level(found.group(1)) if found else None
    if level is None:
        level = "error" if (trace or _EXC_CLASS.search(message)) else "info"

    explicit_type = _as_text(_pick(payload, "type"))
    return NormalizedEvent(
        service=(_as_text(_pick(payload, "service")) or "unknown")[:80],
        event_type=infer_event_type(explicit_type, message, trace),
        level=level,  # type: ignore[arg-type]
        timestamp=timestamp,
        message=message[:MAX_MESSAGE_CHARS],
        trace=trace[:MAX_TRACE_CHARS],
    )
