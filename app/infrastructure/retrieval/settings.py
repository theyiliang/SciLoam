"""Research retrieval configuration. Reading settings never creates files or clients."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


def _http_url(value: str, name: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"Invalid {name}")


@dataclass(frozen=True)
class ResearchRetrievalSettings:
    mode: str = "local"
    allow_external: bool = False
    data_dir: Path = Path("research-data")
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-v4"
    embedding_dim: int = 1024
    embedding_max_batch: int = 10
    reranker_base_url: str = ""
    reranker_api_key: str = ""
    reranker_model: str = ""
    qdrant_url: str = ""
    timeout_seconds: float = 15.0
    max_candidates: int = 32

    def __post_init__(self) -> None:
        import math

        if self.mode not in {"local", "hybrid"}:
            raise ValueError("RESEARCH_RETRIEVAL_MODE must be local or hybrid")
        if not isinstance(self.allow_external, bool):
            raise ValueError("allow_external must be boolean")
        for name, value, upper in (
            ("EMBEDDING_DIM", self.embedding_dim, None),
            ("EMBEDDING_MAX_BATCH", self.embedding_max_batch, 10),
            ("max_candidates", self.max_candidates, 32),
        ):
            if type(value) is not int or value < 1 or (upper is not None and value > upper):
                raise ValueError(f"Invalid {name}")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (float, int)) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        for name in ("embedding_base_url", "reranker_base_url", "qdrant_url"):
            if getattr(self, name):
                _http_url(getattr(self, name), name)

    @property
    def qdrant_path(self) -> Path:
        return Path(self.data_dir) / "qdrant-research"


RetrievalSettings = ResearchRetrievalSettings


# No dependency on the legacy application settings (which load .env at import).
def load_retrieval_settings(environ: Mapping[str, str] | None = None) -> RetrievalSettings:
    env = os.environ if environ is None else environ
    return RetrievalSettings(
        mode=env.get("RESEARCH_RETRIEVAL_MODE", "local").strip().lower(),
        allow_external=env.get("RESEARCH_RAG_ALLOW_EXTERNAL", "") == "1",
        data_dir=Path(env.get("RESEARCH_DATA_DIR", "research-data")),
        embedding_base_url=env.get("EMBEDDING_BASE_URL", "").strip(),
        embedding_api_key=env.get("EMBEDDING_API_KEY", ""),
        embedding_model=env.get("EMBEDDING_MODEL", "text-embedding-v4"),
        embedding_dim=int(env.get("EMBEDDING_DIM", "1024")),
        embedding_max_batch=int(env.get("EMBEDDING_MAX_BATCH", "10")),
        reranker_base_url=env.get("RERANKER_BASE_URL", "").strip(),
        reranker_api_key=env.get("RERANKER_API_KEY", ""),
        reranker_model=env.get("RERANKER_MODEL", ""),
        qdrant_url=env.get("QDRANT_URL", "").strip(),
    )


load_settings = load_retrieval_settings
