"""Embedding: an optional channel that must name itself.

The rule this module exists to enforce: **a configured embedder either works and
identifies itself, or the channel is absent.** There is no fallback model, because a
silent fallback is indistinguishable from working semantic recall until someone
notices the results are nonsense.

Three consequences of that rule:

* :func:`resolve` returns ``None`` rather than a substitute when nothing usable is
  configured, and it reports *why*;
* every stored vector records the embedder that produced it, so vectors of unknown
  provenance can be excluded instead of trusted;
* no embedding dependency is installed or imported at module import time.

The pack format is packed float32, so the channel needs no array library.
"""

from __future__ import annotations

import json
import math
import os
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol, Sequence, runtime_checkable

from .util import utc_now


@runtime_checkable
class Embedder(Protocol):
    """The whole interface: a name that identifies the model, and embed()."""

    name: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class EmbedderStatus:
    """What the kernel decided about the channel, and why."""

    enabled: bool
    name: str
    dimensions: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "name": self.name,
            "dimensions": self.dimensions,
            "reason": self.reason,
        }


def pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *[float(v) for v in vector])


def unpack(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob[: count * 4]))


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


class OpenAICompatEmbedder:
    """Any OpenAI-compatible ``/embeddings`` endpoint, over the standard library."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int = 0,
        timeout: float = 30.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not model:
            raise ValueError("model is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dimensions = int(dimensions)
        self.timeout = float(timeout)
        self.extra_headers = dict(extra_headers or {})
        self.name = f"openai-compatible:{model}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                **self.extra_headers,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        rows = body.get("data") or []
        if len(rows) != len(texts):
            raise ValueError(f"expected {len(texts)} vectors, received {len(rows)}")
        vectors = [list(map(float, row.get("embedding") or [])) for row in rows]
        if not vectors or not vectors[0]:
            raise ValueError("endpoint returned empty vectors")
        if self.dimensions == 0:
            self.dimensions = len(vectors[0])
        if len(vectors[0]) != self.dimensions:
            raise ValueError(
                f"declared {self.dimensions} dimensions, endpoint returned {len(vectors[0])}"
            )
        self.name = f"openai-compatible:{self.model}:{self.dimensions}"
        return vectors


class SentenceTransformerEmbedder:
    """A local sentence-transformers model, loaded offline on purpose.

    The offline flags are set before the import because the hub reads them at import
    time, and ``local_files_only`` is passed explicitly because the constructor is a
    second place that will otherwise reach for the network.
    """

    def __init__(self, *, model: str, device: str = "", batch_size: int = 16) -> None:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415 - optional

        kwargs: dict[str, Any] = {"local_files_only": True}
        if device:
            kwargs["device"] = device
        self._model = SentenceTransformer(model, **kwargs)
        self.model_name = model
        self.batch_size = batch_size
        probe = self._model.encode(["dimension probe"], normalize_embeddings=True)
        self.dimensions = int(len(probe[0]))
        self.name = f"sentence-transformers:{model}:{self.dimensions}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._model.encode(
            list(texts), batch_size=self.batch_size, normalize_embeddings=True
        )
        return [list(map(float, vector)) for vector in vectors]


class DeterministicTestEmbedder:
    """A test double. **Not a semantic model** and never a fallback.

    It maps character n-grams onto a fixed number of buckets so the vector channel
    can be exercised without a model or a network. It is registered only when a
    caller asks for it by name, and it announces itself as a test double in
    :attr:`name`, so a store populated with it cannot be mistaken for one populated
    by a real model.
    """

    def __init__(self, *, dimensions: int = 64) -> None:
        self.dimensions = int(dimensions)
        self.name = f"test-double:char-ngram:{self.dimensions}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            lowered = str(text or "").lower()
            for index in range(len(lowered)):
                gram = lowered[index : index + 2]
                bucket = hash(gram) % self.dimensions
                vector[bucket] += 1.0
            norm = math.sqrt(sum(v * v for v in vector))
            if norm:
                vector = [v / norm for v in vector]
            out.append(vector)
        return out


def resolve(spec: dict[str, Any] | None) -> tuple[Embedder | None, EmbedderStatus]:
    """Build the configured embedder, or explain why the channel is absent.

    Returns ``(None, status)`` when nothing usable is configured. It never returns a
    substitute: an unavailable embedder means the semantic channel does not exist for
    this store, and retrieval proceeds lexically.
    """

    if not spec or not spec.get("provider"):
        return None, EmbedderStatus(False, "", 0, "not_configured")

    provider = str(spec.get("provider")).strip().lower()
    try:
        if provider in ("openai", "openai-compatible", "compatible"):
            key = str(spec.get("api_key") or "")
            if not key:
                env_name = str(spec.get("api_key_env") or "")
                key = os.environ.get(env_name, "") if env_name else ""
            if not key:
                return None, EmbedderStatus(False, "", 0, "missing_api_key")
            embedder = OpenAICompatEmbedder(
                base_url=str(spec.get("base_url") or "https://api.openai.com/v1"),
                api_key=key,
                model=str(spec.get("model") or "text-embedding-3-small"),
                dimensions=int(spec.get("dimensions") or 0),
                timeout=float(spec.get("timeout") or 30.0),
            )
            return embedder, EmbedderStatus(
                True, embedder.name, embedder.dimensions, "openai_compatible"
            )
        if provider in ("sentence-transformers", "local", "st"):
            embedder = SentenceTransformerEmbedder(
                model=str(spec.get("model") or "BAAI/bge-small-zh-v1.5"),
                device=str(spec.get("device") or ""),
                batch_size=int(spec.get("batch_size") or 16),
            )
            return embedder, EmbedderStatus(
                True, embedder.name, embedder.dimensions, "local_model"
            )
        if provider in ("test", "test-double"):
            embedder = DeterministicTestEmbedder(dimensions=int(spec.get("dimensions") or 64))
            return embedder, EmbedderStatus(
                True, embedder.name, embedder.dimensions, "test_double"
            )
    except Exception as exc:  # noqa: BLE001 - report, never substitute
        return None, EmbedderStatus(
            False, "", 0, f"unavailable:{type(exc).__name__}:{str(exc)[:120]}"
        )

    return None, EmbedderStatus(False, "", 0, f"unknown_provider:{provider}")


def describe(embedder: Embedder | None, status: EmbedderStatus) -> dict[str, Any]:
    payload = status.as_dict()
    payload["specified_dimensions"] = getattr(embedder, "dimensions", 0) if embedder else 0
    payload["resolved_at"] = utc_now()
    return payload
