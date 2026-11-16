"""Pure domain and injected-port tests: no SQLite, HTTP, or model provider."""
import ast
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
import re

import pytest

from app.application.usecases.facade import ResearchService
from app.domain import policies
from app.domain.errors import DomainError
from app.domain.models import Conversation, Document, Review, Space, Task, User, Version


def denied(code, action, *args, **kwargs):
    with pytest.raises(DomainError) as error:
        action(*args, **kwargs)
    assert error.value.status_code == code


def test_private_space_does_not_inherit_project_owner_authority():
    owner = User("owner", "owner@example.org", "owner", 1)
    member = User("alice", "alice@example.org", "member", 1)
    private = Space("private", "private", "alice", 1)
    denied(403, private.require_access, owner, None)
    denied(403, private.require_access, owner, "owner")
    assert private.require_access(owner, "reader") == "reader"
    denied(403, private.require_access, owner, "reader", write=True)
    assert private.require_access(member, None, write=True, manage=True) == "owner"
    results = Space("results", "results", "owner", 1)
    denied(403, results.require_access, owner, "owner", write=True)
    private.require_grant("owner", "reader")
    private.require_grant("owner", "editor")
    denied(409, private.require_grant, "alice", "reader")
    denied(422, private.require_grant, "owner", "owner")
    denied(403, results.require_grant, "alice", "editor")
    denied(409, results.require_grant, "owner", "reader")
    denied(403, results.require_revocation, owner)
    shared = Space("shared", "collaboration", "alice", 1)
    shared.require_revocation(owner)
    private.require_revocation(owner)
    denied(403, shared.require_access, owner, None)
    denied(403, shared.require_access, owner, "owner")
    assert shared.require_access(owner, "editor", write=True) == "editor"
    denied(403, shared.require_access, owner, "editor", manage=True)
    denied(403, private.require_revocation, member)
    basis = Space("basis", "basis", "owner", 1)
    for system in (basis, results):
        assert system.require_access(owner, None, manage=True) == "owner"
        assert system.require_access(member, "editor") == "reader"
        denied(403, system.require_access, member, "editor", write=True)
        denied(403, system.require_grant, "alice", "editor")
    denied(401, private.require_access, User("alice", "alice@example.org", "member", 1, 2), "owner")


def test_withdrawal_requires_access_checked_by_usecase_and_domain_authority():
    author = User("alice", "alice@example.org", "member", 1)
    reader = User("bob", "bob@example.org", "member", 1)
    owner = User("owner", "owner@example.org", "owner", 1)
    document = Document("doc", "shared", "alice")
    document.require_withdrawal(author, Space("shared", "collaboration", "owner", 1), "reader")
    denied(403, document.require_withdrawal, reader, Space("shared", "collaboration", "owner", 1), "reader")
    denied(403, document.require_withdrawal, author, Space("results", "results", "owner", 1), "reader")
    document.require_withdrawal(owner, Space("results", "results", "owner", 1), "owner")


def test_review_lineage_idempotency_and_conflicts():
    pending = Review("r", "alice", "pending", None, "")
    denied(403, pending.decision, "owner", "approve", "", {"alice", "owner"})
    denied(403, pending.decision, "alice", "approve", "", {"bob"})
    assert pending.decision("owner", "approve", "", {"alice"}) == ("approved", False)
    approved = Review("r", "alice", "approved", "owner", "evidence")
    assert approved.decision("owner", "approve", "evidence", {"alice"}) == ("approved", True)
    denied(409, approved.decision, "owner", "reject", "evidence", {"alice"})
    denied(409, approved.decision, "owner", "approve", "different", {"alice"})
    denied(422, policies.review_decision, "reject", " ")


def test_task_state_machine_separates_assignee_progress_from_owner_acceptance():
    owner = User("owner", "owner@example.org", "owner", 1)
    assignee = User("alice", "alice@example.org", "member", 1)
    task = Task("task", "alice", "not_started")
    denied(403, task.authorize_edit, assignee, True)
    assert task.transition(assignee, "in_progress") == "in_progress"
    denied(409, task.transition, assignee, "completed")
    denied(403, task.transition, owner, "in_progress")
    pending = Task("task", "alice", "pending_acceptance")
    denied(403, pending.transition, assignee, "completed")
    assert pending.transition(owner, "completed") == "completed"
    denied(409, pending.require_reassignment, "bob")
    denied(403, Task("task", "owner", "pending_acceptance").transition, owner, "completed")
    denied(409, Task("task", "alice", "completed").require_progress, "alice")


def test_immutable_version_and_conversation_epoch():
    version = Version("version", "doc", 1, "evidence", "paper.md", "local", "alice", 1)
    with pytest.raises(FrozenInstanceError):
        version.content = "overwrite"
    detached = version.record()
    detached["content"] = "changed"
    assert version.content == "evidence"
    conversation = Conversation("chat", "alice", 3)
    denied(403, conversation.require_owner, "owner")
    denied(409, conversation.require_epoch, Space("space", "collaboration", "alice", 4))
    assert policies.citation_ids([{"version_id": "version", "role": "owner", "secret": "drop"}]) == ["version"]
    denied(422, policies.citation_ids, [123])
    denied(403, policies.require_citation_space, {"space_id": "private"}, {"space_id": "shared"})


def test_validation_and_ranked_evidence_are_pure():
    assert policies.email(" ALICE@Example.org ") == "alice@example.org"
    assert policies.due_date("2026-12-01") == "2026-12-01"
    denied(422, policies.due_date, "20261201")
    denied(422, policies.password, "short")
    denied(422, policies.body, "body", "unsafe.pdf", "source")
    denied(403, policies.share, {"id": "a", "kind": "private"}, {"id": "b", "kind": "basis"})
    rows = [{"version_id": "v", "title": "锂电池寿命研究", "content": "序言" * 1000 + "锂电池容量寿命证据", "source": "local"}]
    hits = policies.rank_evidence(rows, "space", "锂电池寿命", 5)
    assert hits[0]["space_id"] == "space"
    assert "证据" in hits[0]["content"]
    assert rows[0]["content"].startswith("序言")
    assert policies.rank_evidence(rows, "space", "%%%", 5) == []


class MemoryRepository:
    KEYS = {"sessions": "token_hash", "login_failures": "email", "document_withdrawals": "document_id", "version_indexes": "version_id"}

    def __init__(self):
        self.rows = {}

    def list(self, entity, **filters):
        return [deepcopy(row) for row in self.rows.get(entity, []) if all(row.get(key) == value for key, value in filters.items())]

    def find(self, entity, **filters):
        return next(iter(self.list(entity, **filters)), None)

    def get(self, entity, key):
        return self.find(entity, **{self.KEYS.get(entity, "id"): key})

    def add(self, entity, record):
        record = deepcopy(record)
        if entity == "users":
            record.setdefault("disabled_at", None)
        self.rows.setdefault(entity, []).append(record)

    def update(self, entity, key, changes):
        for row in self.rows.get(entity, []):
            if row.get(self.KEYS.get(entity, "id")) == key:
                row.update(deepcopy(changes))

    def delete_where(self, entity, **filters):
        original = self.rows.get(entity, [])
        remaining = [row for row in original if not all(row.get(key) == value for key, value in filters.items())]
        self.rows[entity] = remaining
        return len(original) - len(remaining)

    def membership(self, space_id, user_id):
        row = self.find("space_members", space_id=space_id, user_id=user_id)
        return row["role"] if row else None

    def set_membership(self, space_id, user_id, role):
        self.delete_where("space_members", space_id=space_id, user_id=user_id)
        self.add("space_members", {"space_id": space_id, "user_id": user_id, "role": role})

    def save_login_failure(self, email, record):
        self.delete_where("login_failures", email=email)
        self.add("login_failures", {"email": email, **record})


class MemoryUow:
    def __init__(self, repository):
        self.repository = repository
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        self.snapshot = deepcopy(self.repository.rows)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type:
            self.repository.rows = self.snapshot
            self.rollbacks += 1
        else:
            self.commits += 1


class TestSecurity:
    __test__ = False

    def __init__(self):
        self.counter = 0

    def hash_password(self, password):
        return "test-password:" + password

    def verify_password(self, password, encoded):
        return encoded == self.hash_password(password)

    def new_token(self):
        self.counter += 1
        return "test-token-" + str(self.counter)

    def hash_token(self, token):
        return "test-digest:" + token


def test_facade_uses_injected_ports_and_clock_without_concrete_adapters():
    repository = MemoryRepository()
    uow = MemoryUow(repository)
    service = ResearchService(lambda: uow, TestSecurity(), lambda: 123.0)
    result = service.bootstrap("owner@example.org", "long-test-password", "Pure project")
    owner = result["user"]["id"]
    assert result["user"]["created_at"] == 123.0
    assert len(service.list_spaces(owner)) == 3
    session = service.login("owner@example.org", "long-test-password")
    assert service.authenticate(session["token"])["id"] == owner
    for _ in range(5):
        denied(401, service.login, "owner@example.org", "bad")
    denied(429, service.login, "owner@example.org", "long-test-password")
    assert repository.get("login_failures", "owner@example.org")["failures"] == 5
    assert uow.commits >= 10
    denied(409, service.bootstrap, "second@example.org", "long-test-password", "No second project")
    assert uow.rollbacks == 1
    service.recover_password("owner@example.org", "replacement-password")
    denied(401, service.authenticate, session["token"])
    assert service.login("owner@example.org", "replacement-password")["user"]["id"] == owner


SQL_STATEMENT = re.compile(
    r'\b(?:SELECT\s+[\s\S]+?\s+FROM\s+[\w"\[(`]|INSERT\s+INTO\s+|'
    r'UPDATE\s+[\w"\[(`]+\s+SET\s+|DELETE\s+FROM\s+)', re.I)


@pytest.mark.parametrize("value,expected", [
    ("Select current basis knowledge before publication", False),
    ("Source is missing; select current basis knowledge", False),
    ("SELECT id FROM versions WHERE id=?", True),
    ("select\n title\nfrom goal_nodes", True),
    ("INSERT INTO goal_nodes (id) VALUES (?)", True),
    ("UPDATE goals SET title=?", True),
    ("DELETE FROM versions WHERE id=?", True),
])
def test_sql_architecture_scanner_distinguishes_error_messages(value, expected):
    assert bool(SQL_STATEMENT.search(value)) is expected


def test_onion_dependency_boundaries_and_no_sql_in_usecases():
    root = Path(__file__).resolve().parents[1]
    forbidden = ("app.infrastructure", "app.research", "fastapi", "agentscope", "sqlite3")
    for package in (root / "app/domain", root / "app/application/usecases"):
        assert package.is_dir(), f"Missing architecture target: {package}"
        paths = list(package.rglob("*.py"))
        assert any(path.name != "__init__.py" for path in paths), f"Empty architecture target: {package}"
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    modules = []
                assert not any(module == prefix or module.startswith(prefix + ".") for module in modules for prefix in forbidden), path
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert not SQL_STATEMENT.search(node.value), path
                if package.name == "domain":
                    assert not any(module.startswith("app.") and not module.startswith("app.domain.") for module in modules), path
