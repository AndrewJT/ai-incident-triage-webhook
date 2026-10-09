"""Stage 2 - context retrieval.

A small local vector index over runbooks (markdown, chunked per `##` section)
and past resolutions (JSONL). Embeddings come from either:

* `HashingEmbedder` - deterministic feature hashing, no model, works offline
  (default; also keeps tests hermetic), or
* `OllamaEmbedder`  - real semantic embeddings via Ollama's `/api/embed`.
"""
from __future__ import annotations

import json
import math
import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this to was were will with".split()
)


def tokenize(text: str) -> list[str]:
    text = _CAMEL.sub(r"\1 \2", text).lower()
    return [t for t in _TOKEN.findall(text) if t not in _STOP and len(t) > 1]


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Signed feature hashing of unigrams + bigrams, L2-normalised."""

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def _one(self, text: str) -> list[float]:
        tokens = tokenize(text)
        vec = [0.0] * self.dim
        features = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
        for feat in features:
            h = zlib.crc32(feat.encode())
            vec[h % self.dim] += 1.0 if (h >> 31) & 1 else -1.0
        return _normalize(vec)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, timeout: float = 30.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self._url = f"{base_url}/api/embed"
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.post(self._url, json={"model": self._model, "input": texts})
        resp.raise_for_status()
        return [_normalize(v) for v in resp.json()["embeddings"]]


@dataclass(frozen=True)
class Chunk:
    id: str
    kind: str  # "runbook" | "resolution"
    title: str
    section: str
    text: str


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


def load_chunks(knowledge_dir: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted((knowledge_dir / "runbooks").glob("*.md")):
        title, section, buf = path.stem, "Overview", []

        def flush() -> None:
            body = "\n".join(buf).strip()
            if body:
                chunks.append(Chunk(f"{path.stem}#{len(chunks)}", "runbook", title, section, body))

        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
            elif line.startswith("## "):
                flush()
                section, buf = line[3:].strip(), []
            else:
                buf.append(line)
        flush()

    resolutions = knowledge_dir / "past_resolutions.jsonl"
    if resolutions.exists():
        for i, line in enumerate(resolutions.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            row = json.loads(line)
            text = f"Symptom: {row['symptom']}\nResolution: {row['resolution']}"
            chunks.append(Chunk(f"resolution#{i}", "resolution", row["title"], "Past resolution", text))
    return chunks


def _embed_text(chunk: Chunk) -> str:
    return f"{chunk.title}. {chunk.section}. {chunk.text}"


class KnowledgeBase:
    def __init__(self, embedder: Embedder, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        self._embedder = embedder
        self._chunks = chunks
        self._vectors = vectors

    @classmethod
    async def build(cls, knowledge_dir: Path, embedder: Embedder) -> "KnowledgeBase":
        chunks = load_chunks(knowledge_dir)
        vectors = await embedder.embed([_embed_text(c) for c in chunks]) if chunks else []
        return cls(embedder, chunks, vectors)

    def __len__(self) -> int:
        return len(self._chunks)

    async def retrieve(self, query: str, k: int, min_score: float) -> list[Hit]:
        if not self._chunks:
            return []
        (qv,) = await self._embedder.embed([query])
        scored = [Hit(c, sum(a * b for a, b in zip(qv, v))) for c, v in zip(self._chunks, self._vectors)]
        scored.sort(key=lambda h: h.score, reverse=True)
        return [h for h in scored[:k] if h.score >= min_score]

    def remediation_for(self, title: str) -> list[str]:
        """Bullet/numbered lines of a runbook's `Remediation` section."""
        for c in self._chunks:
            if c.kind == "runbook" and c.title == title and c.section.lower().startswith("remediation"):
                steps = [re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", ln).strip()
                         for ln in c.text.splitlines() if re.match(r"^\s*(?:[-*]|\d+[.)])\s+", ln)]
                return steps
        return []
