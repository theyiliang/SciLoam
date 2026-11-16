"""Transport-neutral compatibility facade, composed from focused use cases.

All adapters are constructor-injected. Each action owns its unit of work; no
connection, SQL, transport, model-provider or concrete adapter is created here.
"""
from .common import ResearchContext
from .conversations import ConversationUseCases
from .documents import DocumentUseCases
from .identity import IdentityUseCases
from .goals import GoalUseCases
from .goal_drafts import GoalDraftUseCases
from .reviews import ReviewUseCases
from .spaces import SpaceUseCases
from .tasks import TaskUseCases


class ResearchService(
    IdentityUseCases,
    SpaceUseCases,
    DocumentUseCases,
    ReviewUseCases,
    TaskUseCases,
    GoalUseCases,
    GoalDraftUseCases,
    ConversationUseCases,
    ResearchContext,
):
    """Existing research API contract with UoW/security/clock injection."""

    def __init__(self, *args, goal_generator=None, allow_experimental_goal_generator=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._goal_generator = goal_generator
        # Explicit offline experiments only; no environment variable enables this.
        self._allow_experimental_goal_generator = allow_experimental_goal_generator
