"""Stage 3 - structured inference against a local Ollama model.

Reliability layers, in order:
1. Constrained decoding: the `Triage` JSON schema is passed as Ollama's `format`.
2. Defensive extraction: strip markdown fences / surrounding prose if any leak through.
3. Strict validation: pydantic with `extra=forbid` rejects hallucinated keys.
4. Repair retry: the validation error is fed back to the model once.
5. Caller falls back to the deterministic parser if all of the above fail.
"""
from __future__ import annotations

import json
import re
from typing import Protocol

import httpx
from pydantic import ValidationError

from .config import Settings
from .models import NormalizedEvent, Triage
from .prompt import SYSTEM_PROMPT, build_user_prompt


class TriageError(Exception):
    """LLM could not produce a valid `Triage` (unreachable, timeout, or invalid output)."""


class TriageClient(Protocol):
    async def triage(self, event: NormalizedEvent, context: str) -> Triage: ...


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def extract_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating fences and stray prose."""
    cleaned = _FENCE.sub("", text.strip()).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in model output") from None
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("model output is not a JSON object")
    return value


class OllamaTriageClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._client = client or httpx.AsyncClient(timeout=settings.llm_timeout_s)
        self._schema = Triage.model_json_schema()

    async def _chat(self, messages: list[dict]) -> str:
        resp = await self._client.post(
            f"{self._s.ollama_url}/api/chat",
            json={
                "model": self._s.llm_model,
                "messages": messages,
                "stream": False,
                "format": self._schema,  # constrained decoding
                "options": {"temperature": 0, "num_ctx": self._s.num_ctx},
            },
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    async def triage(self, event: NormalizedEvent, context: str) -> Triage:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(
                event, context, self._s.max_trace_lines, self._s.max_trace_chars)},
        ]
        last_error: Exception | None = None
        for _ in range(self._s.llm_max_retries + 1):
            try:
                content = await self._chat(messages)
            except httpx.ConnectError as exc:
                # Backend is down: retrying cannot help, go straight to the fallback.
                raise TriageError(f"Ollama unreachable at {self._s.ollama_url}: {exc}") from exc
            except (httpx.HTTPError, KeyError, json.JSONDecodeError) as exc:
                last_error = exc
                continue
            try:
                return Triage.model_validate(extract_json(content))
            except (ValueError, ValidationError) as exc:
                last_error = exc
                messages += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": (
                        f"Your reply was rejected: {str(exc)[:400]}\n"
                        "Reply again with ONLY a valid JSON object using exactly the required keys.")},
                ]
        raise TriageError(f"{type(last_error).__name__}: {last_error}") from last_error
