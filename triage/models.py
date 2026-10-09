"""Typed domain models. `Triage` doubles as the JSON schema handed to Ollama."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Level = Literal["debug", "info", "warning", "error", "critical"]
Severity = Literal["critical", "high", "medium", "low"]


class NormalizedEvent(BaseModel):
    """Canonical event produced by the validation & extraction stage."""

    service: str = "unknown"
    event_type: str
    level: Level
    timestamp: datetime
    message: str
    trace: str = ""


class Triage(BaseModel):
    """Strictly typed LLM output. Unknown keys are rejected (`extra=forbid`),
    which is what catches hallucinated fields."""

    model_config = ConfigDict(extra="forbid")

    severity: Severity
    failure_mode: str = Field(min_length=2, max_length=120)
    root_cause_probability: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=1, max_length=600)
    remediation_steps: list[str] = Field(min_length=1, max_length=8)

    @field_validator("remediation_steps")
    @classmethod
    def _clean_steps(cls, steps: list[str]) -> list[str]:
        cleaned = [s.strip() for s in steps if isinstance(s, str) and s.strip()]
        if not cleaned:
            raise ValueError("remediation_steps must contain at least one non-empty step")
        if any(len(s) > 400 for s in cleaned):
            raise ValueError("remediation step too long")
        return cleaned
