"""Runtime configuration, read from environment variables (12-factor style)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _opt(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    # --- LLM / embeddings (Ollama) -------------------------------------
    ollama_url: str = "http://127.0.0.1:11434"
    llm_model: str = "mistral"
    embed_model: str = "nomic-embed-text"
    embedding_backend: str = "hashing"  # "hashing" (offline, zero deps) | "ollama"
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 1
    num_ctx: int = 4096

    # --- Retrieval & prompt budget -------------------------------------
    knowledge_dir: Path = ROOT / "knowledge"
    top_k: int = 3
    min_score: float = 0.12
    max_context_chars: int = 3000
    max_trace_lines: int = 30
    max_trace_chars: int = 2500

    # --- Ingestion / queue ---------------------------------------------
    queue_size: int = 256
    workers: int = 2
    dedup_ttl_s: float = 60.0
    max_body_bytes: int = 64 * 1024
    webhook_secret: str | None = None

    # --- Dispatch ------------------------------------------------------
    chat_webhook_url: str | None = None  # Slack-compatible incoming webhook
    ticket_api_url: str | None = None
    ticket_api_token: str | None = None
    outbox_path: Path = ROOT / "outbox" / "dispatch.jsonl"
    notify_min_severity: str = "medium"
    ticket_min_severity: str = "medium"
    dispatch_retries: int = 3

    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        d = cls()
        return cls(
            ollama_url=_env("OLLAMA_URL", d.ollama_url).rstrip("/"),
            llm_model=_env("LLM_MODEL", d.llm_model),
            embed_model=_env("EMBED_MODEL", d.embed_model),
            embedding_backend=_env("EMBEDDING_BACKEND", d.embedding_backend),
            llm_timeout_s=float(_env("LLM_TIMEOUT_S", str(d.llm_timeout_s))),
            llm_max_retries=int(_env("LLM_MAX_RETRIES", str(d.llm_max_retries))),
            num_ctx=int(_env("NUM_CTX", str(d.num_ctx))),
            knowledge_dir=Path(_env("KNOWLEDGE_DIR", str(d.knowledge_dir))),
            top_k=int(_env("TOP_K", str(d.top_k))),
            min_score=float(_env("MIN_SCORE", str(d.min_score))),
            max_context_chars=int(_env("MAX_CONTEXT_CHARS", str(d.max_context_chars))),
            max_trace_lines=int(_env("MAX_TRACE_LINES", str(d.max_trace_lines))),
            max_trace_chars=int(_env("MAX_TRACE_CHARS", str(d.max_trace_chars))),
            queue_size=int(_env("QUEUE_SIZE", str(d.queue_size))),
            workers=int(_env("WORKERS", str(d.workers))),
            dedup_ttl_s=float(_env("DEDUP_TTL_S", str(d.dedup_ttl_s))),
            max_body_bytes=int(_env("MAX_BODY_BYTES", str(d.max_body_bytes))),
            webhook_secret=_opt("WEBHOOK_SECRET"),
            chat_webhook_url=_opt("CHAT_WEBHOOK_URL"),
            ticket_api_url=_opt("TICKET_API_URL"),
            ticket_api_token=_opt("TICKET_API_TOKEN"),
            outbox_path=Path(_env("OUTBOX_PATH", str(d.outbox_path))),
            notify_min_severity=_env("NOTIFY_MIN_SEVERITY", d.notify_min_severity),
            ticket_min_severity=_env("TICKET_MIN_SEVERITY", d.ticket_min_severity),
            dispatch_retries=int(_env("DISPATCH_RETRIES", str(d.dispatch_retries))),
        )
