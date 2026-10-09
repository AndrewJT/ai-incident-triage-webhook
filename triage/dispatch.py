"""Stage 4 - dispatch: tracked ticket via REST + chat alert.

If `TICKET_API_URL` / `CHAT_WEBHOOK_URL` are unset, payloads are appended to a
local JSONL outbox instead, so the whole flow is demo-able without any
external system. Dispatch failures never crash the pipeline; they are reported.
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import httpx

from .config import SEVERITY_RANK, Settings
from .models import NormalizedEvent, Triage


class Dispatcher:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._lock = threading.Lock()

    # -- helpers --------------------------------------------------------
    async def _post_json(self, url: str, payload: dict, headers: dict | None = None) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self._s.dispatch_retries):
            try:
                resp = await self._client.post(url, json=payload, headers=headers)
                if resp.status_code < 500:
                    resp.raise_for_status()
                    return resp
                last = httpx.HTTPStatusError("server error", request=resp.request, response=resp)
            except httpx.HTTPStatusError:
                raise  # 4xx: retrying will not help
            except httpx.HTTPError as exc:
                last = exc
            await asyncio.sleep(0.5 * 2**attempt)
        raise last or RuntimeError("dispatch failed")

    def _write_outbox(self, kind: str, payload: dict) -> None:
        path = self._s.outbox_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"channel": kind, "payload": payload}, ensure_ascii=False) + "\n")

    @staticmethod
    def _enabled(triage: Triage, minimum: str) -> bool:
        return SEVERITY_RANK[triage.severity] >= SEVERITY_RANK.get(minimum, 0)

    # -- ticket ---------------------------------------------------------
    async def _create_ticket(self, event_id: str, event: NormalizedEvent, triage: Triage, source: str) -> dict[str, Any]:
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(triage.remediation_steps, 1))
        payload = {
            "title": f"[{triage.severity.upper()}] {event.service}: {triage.failure_mode}",
            "description": (f"{triage.summary}\n\nRoot-cause probability: {triage.root_cause_probability:.0%}\n\n"
                            f"Remediation:\n{steps}\n\nOriginal message: {event.message}"),
            "priority": triage.severity,
            "labels": ["auto-triage", event.service, event.event_type, f"analysis:{source}"],
            "metadata": {"event_id": event_id, "timestamp": event.timestamp.isoformat()},
        }
        if not self._s.ticket_api_url:
            self._write_outbox("ticket", payload)
            return {"mode": "outbox", "id": f"LOCAL-{event_id[:6].upper()}"}
        headers = {"Authorization": f"Bearer {self._s.ticket_api_token}"} if self._s.ticket_api_token else None
        body = (await self._post_json(self._s.ticket_api_url, payload, headers)).json()
        return {"mode": "http", "id": body.get("id") or body.get("key"), "url": body.get("url")}

    # -- chat -----------------------------------------------------------
    async def _notify(self, event: NormalizedEvent, triage: Triage, ticket: dict | None) -> dict[str, Any]:
        ticket_ref = f" | ticket {ticket['id']}" if ticket and ticket.get("id") else ""
        text = (f"*[{triage.severity.upper()}]* `{event.service}` - {triage.failure_mode} "
                f"(p={triage.root_cause_probability:.2f}){ticket_ref}\n{triage.summary}\n"
                f"First step: {triage.remediation_steps[0]}")
        if not self._s.chat_webhook_url:
            self._write_outbox("chat", {"text": text})
            return {"mode": "outbox", "ok": True}
        await self._post_json(self._s.chat_webhook_url, {"text": text})
        return {"mode": "http", "ok": True}

    # -- public ---------------------------------------------------------
    async def dispatch(self, event_id: str, event: NormalizedEvent, triage: Triage, source: str) -> dict[str, Any]:
        report: dict[str, Any] = {"ticket": None, "notification": None, "errors": []}
        if self._enabled(triage, self._s.ticket_min_severity):
            try:
                report["ticket"] = await self._create_ticket(event_id, event, triage, source)
            except Exception as exc:  # noqa: BLE001 - report, don't crash the worker
                report["errors"].append(f"ticket: {type(exc).__name__}: {exc}")
        if self._enabled(triage, self._s.notify_min_severity):
            try:
                report["notification"] = await self._notify(event, triage, report["ticket"])
            except Exception as exc:  # noqa: BLE001
                report["errors"].append(f"notify: {type(exc).__name__}: {exc}")
        return report
