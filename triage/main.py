"""FastAPI entrypoint: POST /webhook/telemetry -> 202, processed asynchronously."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .dispatch import Dispatcher
from .knowledge import Embedder, HashingEmbedder, KnowledgeBase, OllamaEmbedder
from .llm import OllamaTriageClient, TriageClient
from .normalize import InvalidEvent, normalize
from .pipeline import Pipeline
from .queueing import Metrics, QueueFull, TriageQueue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(
    settings: Settings | None = None,
    llm: TriageClient | None = None,
    embedder: Embedder | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    s = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = http_client or httpx.AsyncClient(timeout=s.llm_timeout_s)
        emb = embedder or (
            OllamaEmbedder(s.ollama_url, s.embed_model, client=client)
            if s.embedding_backend == "ollama" else HashingEmbedder()
        )
        kb = await KnowledgeBase.build(s.knowledge_dir, emb)
        logging.getLogger("triage").info("knowledge base ready: %d chunks", len(kb))
        pipeline = Pipeline(s, kb, llm or OllamaTriageClient(s, client), Dispatcher(s, client))
        app.state.metrics = Metrics()
        app.state.queue = TriageQueue(pipeline, app.state.metrics, s.queue_size, s.workers, s.dedup_ttl_s)
        await app.state.queue.start()
        yield
        await app.state.queue.stop()
        if http_client is None:
            await client.aclose()

    app = FastAPI(title="AI Incident Triage Webhook", version="1.0.0", lifespan=lifespan)

    def verify_signature(body: bytes, header: str | None) -> None:
        if not s.webhook_secret:
            return
        expected = "sha256=" + hmac.new(s.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not header or not hmac.compare_digest(expected, header):
            raise HTTPException(401, "invalid signature")

    @app.post("/webhook/telemetry", status_code=202)
    async def ingest(request: Request):
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > s.max_body_bytes:
            raise HTTPException(413, "payload too large")
        body = await request.body()
        if len(body) > s.max_body_bytes:
            raise HTTPException(413, "payload too large")
        verify_signature(body, request.headers.get("x-signature-256"))

        text = body.decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = text  # raw log line / stack trace
        try:
            event = normalize(payload)
        except InvalidEvent as exc:
            app.state.metrics.counts["invalid"] += 1
            raise HTTPException(422, str(exc)) from exc

        try:
            event_id, status = app.state.queue.submit(event)
        except QueueFull:
            return JSONResponse({"detail": "queue full, retry later"}, status_code=429,
                                headers={"Retry-After": "5"})
        return {"event_id": event_id, "status": status}

    @app.get("/events/{event_id}")
    async def get_event(event_id: str):
        result = app.state.queue.get(event_id)
        if result is None:
            raise HTTPException(404, "unknown event id")
        return result

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics():
        return app.state.metrics.snapshot(app.state.queue.depth)

    return app


app = create_app()  # `uvicorn triage.main:app`
