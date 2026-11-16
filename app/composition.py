"""Composition root: the only layer that assembles concrete research adapters."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.infrastructure.ai.factory import build_assistant
from app.infrastructure.ai.model_gateway import ResearchAISettings
from app.infrastructure.persistence.factory import build_research_service
from app.infrastructure.persistence.research_engineering import SQLiteResearchEngineering
from app.infrastructure.persistence.security_log import SecurityEventLog
from app.application.usecases.retrieval import ResearchRetrieval
from app.infrastructure.retrieval import build_retrieval, load_retrieval_settings
from app.application.usecases.chat_runs import RunManager
from app.application.usecases.goal_generation_runs import GoalGenerationRuns
from app.infrastructure.events.goal_generation_bus import GoalGenerationBus
from app.infrastructure.events.run_bus import InMemoryRunBus
from app.application.usecases.knowledge_import import KnowledgeImport
from app.infrastructure.documents.text_import import TextImportParser


@dataclass
class Container:
    service: Any
    assistant: Any
    engineering: SQLiteResearchEngineering
    runs: Any = None
    importer: Any = None
    goal_runs: Any = None
    vector_index: Any = None
    retrieval_clients: tuple = ()

    def close(self) -> None:
        for client in self.retrieval_clients:
            if client is not None:
                client.close()


def build_container(db_path: Path) -> Container:
    retrieval_settings = load_retrieval_settings()
    embedding, index, reranker = build_retrieval(retrieval_settings)
    retrieval = ResearchRetrieval(embedding, index, reranker,
                                  vector_candidates=retrieval_settings.max_candidates,
                                  lexical_candidates=retrieval_settings.max_candidates)
    # AI goal generation is paused in the product regardless of LLM_API_KEY.
    # The independent question-answering assistant remains enabled below.
    service = build_research_service(db_path, retrieval=retrieval)
    engineering = SQLiteResearchEngineering(db_path)
    ai_settings = ResearchAISettings.from_env()
    assistant = build_assistant(service, settings=ai_settings,
                                security_recorder=SecurityEventLog(db_path).record)
    # The run deadline also covers retrieval/queueing before the AI pipeline
    # starts. Keep a finite 30-second allowance above the same pipeline budget.
    runs = RunManager(service, assistant, InMemoryRunBus(),
                      execution_timeout_seconds=ai_settings.pipeline_timeout_seconds + 30,
                      security_recorder=SecurityEventLog(db_path).record)
    importer = KnowledgeImport(service, TextImportParser())
    return Container(service=service, assistant=assistant,
                     engineering=engineering, runs=runs, importer=importer,
                     goal_runs=GoalGenerationRuns(service, GoalGenerationBus()),
                     vector_index=index, retrieval_clients=(reranker, embedding, index))
