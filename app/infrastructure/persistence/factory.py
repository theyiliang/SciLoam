"""Infrastructure assembly entry point for the application's composition root."""
from pathlib import Path
import time

from app.application.usecases.facade import ResearchService
from app.infrastructure.security.research_passwords import ResearchPasswordSecurity

from .database import Database
from .unit_of_work import SQLiteResearchUnitOfWork


def build_research_service(db_path: str | Path, *, retrieval=None, goal_generator=None) -> ResearchService:
    database = Database(Path(db_path))
    service = ResearchService(
        uow_factory=lambda: SQLiteResearchUnitOfWork(database),
        security=ResearchPasswordSecurity(),
        clock=time.time,
        retrieval=retrieval,
        goal_generator=goal_generator,
    )
    # Backwards-compatible storage inspection for local integration tests only.
    # No use case depends on either attribute; remote adapters must not use them.
    service.db = database
    service.db_path = database.path
    return service
