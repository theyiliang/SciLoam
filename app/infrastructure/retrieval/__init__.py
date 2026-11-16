"""Explicit-consent, synchronous research retrieval infrastructure."""
from .factory import build_retrieval
from .settings import ResearchRetrievalSettings, RetrievalSettings, load_retrieval_settings

__all__ = ["ResearchRetrievalSettings", "RetrievalSettings", "load_retrieval_settings", "build_retrieval"]
