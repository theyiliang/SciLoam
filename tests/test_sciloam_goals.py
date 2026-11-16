"""Goal planning rules, live knowledge ACLs and immutable contribution history."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from app.application.usecases.facade import ResearchService
from app.domain.errors import DomainError
from app.infrastructure.persistence.factory import build_research_service
from tests.test_research_domain import MemoryRepository, MemoryUow, TestSecurity
from tests.test_research_service import denied, shared, space_of, world


def transition(service, owner, goal_id, status, reason=""):
    goal = service.get_goal(owner, goal_id)
    return service.confirm_goal_status(owner, goal_id, status, reason, goal["revision"])


def complete(service, owner, goal_id):
    transition(service, owner, goal_id, "in_progress")
    transition(service, owner, goal_id, "pending_acceptance")
    return transition(service, owner, goal_id, "completed", "Acceptance criteria verified")


@pytest.fixture
def pure_world():
    repository = MemoryRepository()
    uow = MemoryUow(repository)
    service = ResearchService(lambda: uow, TestSecurity(), lambda: 123.0)
    for actor, role in (("owner", "owner"), ("alice", "member"), ("bob", "member")):
        repository.add("users", {"id": actor, "email": actor + "@example.org", "role": role, "created_at": 1})
    return service, repository, uow


def test_goal_usecases_are_injected_port_only_and_member_contributions_are_public(pure_world):
    service, repository, uow = pure_world
    goal = service.create_goal("owner", " Research ", "Public description", acceptance_criteria="Validate evidence")
    assert goal["title"] == "Research" and goal["revision"] == 1
    assert goal["path_titles"] == ["Research"] and goal["path"] == "Research"
    assert goal["can_manage"] and goal["can_contribute"]
    update = service.add_goal_update("alice", goal["id"], "Member-visible research progress")
    detail = service.get_goal("bob", goal["id"])
    assert detail["description"] == "Public description" and detail["updates"] == [update]
    assert not detail["can_manage"] and detail["can_contribute"]
    assert detail["status"] == "not_started" and detail["revision"] == 1
    for actor in ("alice", "bob"):
        denied(403, service.create_goal, actor, "Cannot create")
        denied(403, service.update_goal, actor, goal["id"], 1, title="Cannot edit")
        denied(403, service.confirm_goal_status, actor, goal["id"], "in_progress", "", 1)
        denied(403, service.archive_goal, actor, goal["id"], 1, "Cannot archive")
    assert uow.commits > 0 and uow.rollbacks == 8
    assert repository.get("goal_nodes", goal["id"])["created_at"] == 123.0


def test_flat_tree_paths_preorder_position_and_metadata_updates(pure_world):
    service, repository, uow = pure_world
    later = service.create_goal("owner", "Later", position=2)
    root = service.create_goal("owner", "Root", position=0)
    child = service.create_goal("owner", "Child", parent_id=root["id"], position=10)
    sibling = service.create_goal("owner", "Sibling", parent_id=root["id"], position=0)
    assert [row["id"] for row in service.list_goals("alice")] == [root["id"], sibling["id"], child["id"], later["id"]]
    assert child["path_titles"] == ["Root", "Child"]
    edited = service.update_goal("owner", child["id"], 1, title="Moved", parent_id=later["id"],
                                 description="Shared", acceptance_criteria="Tests pass", due_date="2026-12-31", position=10000)
    assert edited["path"] == "Later / Moved" and edited["revision"] == 2
    assert edited["acceptance_criteria"] == "Tests pass"
    assert service.update_goal("owner", child["id"], 2, due_date=None)["due_date"] is None
    assert service.get_goal("alice", later["id"])["status"] == "not_started"
    # An empty patch does not manufacture a new revision or audit record.
    assert service.update_goal("owner", child["id"], 3)["revision"] == 3


def test_tree_rejects_cycles_missing_or_archived_parent_and_subtree_overdepth(pure_world):
    service, repository, uow = pure_world
    root = service.create_goal("owner", "Root")
    child = service.create_goal("owner", "Child", parent_id=root["id"])
    denied(409, service.update_goal, "owner", root["id"], 1, parent_id=root["id"])
    denied(409, service.update_goal, "owner", root["id"], 1, parent_id=child["id"])
    denied(404, service.create_goal, "owner", "Missing", parent_id="missing")
    chain = [root, child]
    for index in range(2, 8):
        chain.append(service.create_goal("owner", str(index), parent_id=chain[-1]["id"]))
    assert len(service.list_goals("alice")[-1]["path_titles"]) == 8
    denied(409, service.create_goal, "owner", "Too deep", parent_id=chain[-1]["id"])
    subtree = service.create_goal("owner", "Subtree")
    service.create_goal("owner", "Subtree child", parent_id=subtree["id"])
    denied(409, service.update_goal, "owner", subtree["id"], 1, parent_id=chain[-1]["id"])
    assert service.get_goal("owner", subtree["id"])["parent_id"] is None
    archived = service.archive_goal("owner", chain[-1]["id"], 1, "Unneeded leaf")
    denied(409, service.create_goal, "owner", "Archived parent", parent_id=archived["id"])


@pytest.mark.parametrize("position", [-1, 10001, True, 1.5, "1", None])
def test_goal_positions_require_bounded_integers(pure_world, position):
    service, repository, uow = pure_world
    denied(422, service.create_goal, "owner", "Goal", position=position)
    goal = service.create_goal("owner", "Goal")
    denied(422, service.update_goal, "owner", goal["id"], 1, position=position)


def test_revision_staleness_blocks_every_owner_mutation_atomically(pure_world):
    service, repository, uow = pure_world
    goal = service.create_goal("owner", "Goal")
    service.update_goal("owner", goal["id"], 1, title="Current")
    snapshot = deepcopy(repository.rows)
    denied(409, service.update_goal, "owner", goal["id"], 1, title="Stale")
    denied(409, service.confirm_goal_status, "owner", goal["id"], "in_progress", "", 1)
    denied(409, service.archive_goal, "owner", goal["id"], 1, "Stale archive")
    assert repository.rows == snapshot
    for invalid in (True, 0, -1, None, "2"):
        denied(422, service.update_goal, "owner", goal["id"], invalid, title="Invalid revision")


def test_owner_confirmed_status_graph_acceptance_children_and_reopen(pure_world):
    service, repository, uow = pure_world
    parent = service.create_goal("owner", "Parent", acceptance_criteria="Explicit outcome")
    child = service.create_goal("owner", "Child", parent_id=parent["id"])
    ignored = service.create_goal("owner", "Archived child", parent_id=parent["id"])
    service.archive_goal("owner", ignored["id"], 1, "Out of scope")
    denied(409, service.confirm_goal_status, "owner", parent["id"], "completed", "Cannot skip", 1)
    transition(service, "owner", parent["id"], "paused")
    transition(service, "owner", parent["id"], "in_progress")
    transition(service, "owner", parent["id"], "pending_acceptance")
    denied(422, transition, service, "owner", parent["id"], "completed", " ")
    denied(409, transition, service, "owner", parent["id"], "completed", "Acceptance checked")
    complete(service, "owner", child["id"])
    assert service.get_goal("owner", parent["id"])["status"] == "pending_acceptance"
    accepted = transition(service, "owner", parent["id"], "completed", "All explicit outcomes verified")
    assert not accepted["can_contribute"] and accepted["can_manage"]
    assert accepted["status_history"][-1]["reason"] == "All explicit outcomes verified"
    denied(422, transition, service, "owner", parent["id"], "in_progress", "")
    reopened = transition(service, "owner", parent["id"], "in_progress", "New requirement: rerun validation")
    assert reopened["status"] == "in_progress" and reopened["can_contribute"]
    assert len(reopened["status_history"]) == 5
    assert reopened["status_history"][-1]["from_status"] == "completed"
    denied(409, transition, service, "owner", parent["id"], "in_progress", "No-op")
    denied(422, transition, service, "owner", parent["id"], "automatic", "Invalid")


def test_completed_parent_rejects_new_and_moved_children_without_automatic_reopening(pure_world):
    service, repository, uow = pure_world
    parent = service.create_goal("owner", "Completed parent")
    complete(service, "owner", parent["id"])
    movable = service.create_goal("owner", "Movable")
    snapshot = deepcopy(repository.rows)
    denied(409, service.create_goal, "owner", "New child", parent_id=parent["id"])
    denied(409, service.update_goal, "owner", movable["id"], 1, parent_id=parent["id"])
    assert repository.rows == snapshot
    assert service.get_goal("owner", parent["id"])["status"] == "completed"
    assert service.get_goal("owner", movable["id"])["parent_id"] is None
    transition(service, "owner", parent["id"], "in_progress", "Explicitly reopen to expand the plan")
    moved = service.update_goal("owner", movable["id"], 1, parent_id=parent["id"])
    assert moved["parent_id"] == parent["id"]
    assert service.get_goal("owner", parent["id"])["status"] == "in_progress"


def test_reopening_descendant_requires_explicit_top_down_ancestor_reopening(pure_world):
    service, repository, uow = pure_world
    root = service.create_goal("owner", "Root")
    parent = service.create_goal("owner", "Parent", parent_id=root["id"])
    child = service.create_goal("owner", "Child", parent_id=parent["id"])
    for goal in (child, parent, root):
        complete(service, "owner", goal["id"])
    snapshot = deepcopy(repository.rows)
    denied(409, transition, service, "owner", child["id"], "in_progress", "Need to revise")
    denied(409, transition, service, "owner", parent["id"], "in_progress", "Need to revise")
    assert repository.rows == snapshot
    transition(service, "owner", root["id"], "in_progress", "Explicitly reopen root")
    denied(409, transition, service, "owner", child["id"], "in_progress", "Parent is still completed")
    assert service.get_goal("owner", child["id"])["status"] == "completed"
    transition(service, "owner", parent["id"], "in_progress", "Explicitly reopen parent")
    assert service.get_goal("owner", child["id"])["status"] == "completed"
    reopened = transition(service, "owner", child["id"], "in_progress", "Explicitly reopen child")
    assert reopened["status"] == "in_progress"
    assert len(reopened["status_history"]) == 4
    assert service.get_goal("owner", parent["id"])["status"] == "in_progress"
    assert service.get_goal("owner", root["id"])["status"] == "in_progress"


def test_archive_is_leaf_only_revisions_preserves_history_and_blocks_contributions(pure_world):
    service, repository, uow = pure_world
    parent = service.create_goal("owner", "Parent")
    child = service.create_goal("owner", "Child", parent_id=parent["id"])
    entry = service.add_goal_update("alice", child["id"], "Retained progress")
    denied(409, service.archive_goal, "owner", parent["id"], 1, "Has children")
    denied(422, service.archive_goal, "owner", child["id"], 1, " ")
    archived = service.archive_goal("owner", child["id"], 1, "No longer relevant")
    assert archived["revision"] == 2 and archived["archived_at"] == 123.0
    assert not archived["can_manage"] and not archived["can_contribute"]
    assert archived["updates"] == [entry] and archived["status_history"] == []
    assert service.list_goals("bob")[0]["id"] == parent["id"] and len(service.list_goals("bob")) == 1
    denied(409, service.update_goal, "owner", child["id"], 2, title="Rewrite")
    denied(409, service.confirm_goal_status, "owner", child["id"], "in_progress", "", 2)
    denied(409, service.add_goal_update, "alice", child["id"], "No")
    denied(409, service.link_goal_knowledge, "alice", child["id"], "anything")
    denied(409, service.unlink_goal_knowledge, "owner", child["id"], "anything")
    service.archive_goal("owner", parent["id"], 1, "Archived children")
    assert service.list_goals("alice") == []
    assert service.get_goal("bob", child["id"])["updates"] == [entry]
    assert repository.get("goal_nodes", child["id"]) is not None
    assert repository.find("audit_events", action="goal_archived", resource_id=child["id"])


def test_private_knowledge_link_never_grants_acl_or_exposes_hidden_ids_titles_counts(world):
    service, owner, alice, bob = world
    goal = service.create_goal(owner, "Shared goal", "Members can read this description")
    private = space_of(service, alice, "private")
    document = service.add_document(alice, private["id"], "SECRET_TITLE", "private evidence")
    version_id = document["latest_version"]["id"]
    public_update = service.add_goal_update(alice, goal["id"], "Project-wide progress")
    private_update = service.add_goal_update(alice, goal["id"], "SECRET_UPDATE", version_id)
    link = service.link_goal_knowledge(alice, goal["id"], version_id)
    assert link["title"] == "SECRET_TITLE" and "content" not in link
    for actor in (owner, bob):
        denied(403, service.get_version, actor, version_id)
        detail = service.get_goal(actor, goal["id"])
        assert detail["knowledge"] == [] and detail["updates"] == [public_update]
        assert "SECRET_TITLE" not in str(detail) and version_id not in str(detail)
        assert "SECRET_UPDATE" not in str(detail) and private_update["id"] not in str(detail)
        assert not any("count" in key or "hidden" in key for key in detail)
        denied(403, service.link_goal_knowledge, actor, goal["id"], version_id)
    assert service.get_goal(alice, goal["id"])["updates"][-1]["citation"]["version_id"] == version_id
    service.grant_space(alice, private["id"], bob, "reader")
    repeated = service.link_goal_knowledge(bob, goal["id"], version_id)
    assert repeated["id"] == link["id"] and repeated["linked_by"] == alice
    assert len(service.get_goal(bob, goal["id"])["knowledge"]) == 1
    denied(403, service.unlink_goal_knowledge, bob, goal["id"], link["id"])
    service.revoke_space(alice, private["id"], bob)
    assert service.get_goal(bob, goal["id"])["knowledge"] == []
    # A project owner cannot use guessed ids to remove another author's hidden
    # private link, and source errors do not reveal its lifecycle.
    denied(403, service.unlink_goal_knowledge, owner, goal["id"], link["id"])
    service.grant_space(alice, private["id"], owner, "reader")
    assert service.unlink_goal_knowledge(owner, goal["id"], link["id"]) == {"unlinked": True}
    service.revoke_space(alice, private["id"], owner)
    denied(403, service.get_version, owner, version_id)


def test_author_unlink_scope_and_completed_contribution_rejection(world):
    service, owner, alice, bob = world
    space = shared(world)
    version = service.add_document(alice, space["id"], "Public evidence", "evidence")["latest_version"]["id"]
    first = service.create_goal(owner, "First")
    second = service.create_goal(owner, "Second")
    link = service.link_goal_knowledge(alice, first["id"], version)
    denied(404, service.unlink_goal_knowledge, alice, second["id"], link["id"])
    assert service.unlink_goal_knowledge(alice, first["id"], link["id"]) == {"unlinked": True}
    link = service.link_goal_knowledge(alice, first["id"], version)
    complete(service, owner, first["id"])
    denied(409, service.add_goal_update, bob, first["id"], "Complete")
    denied(409, service.link_goal_knowledge, bob, first["id"], version)
    denied(409, service.unlink_goal_knowledge, owner, first["id"], link["id"])
    assert service.get_goal(owner, first["id"])["knowledge"][0]["id"] == link["id"]


@pytest.mark.parametrize("lifecycle", ["withdraw_version", "delete_version", "withdraw_document", "delete_space"])
def test_live_source_lifecycle_hides_whole_bound_update_but_retains_history(world, lifecycle):
    service, owner, alice, bob = world
    space = shared(world)
    document = service.add_document(alice, space["id"], "Evidence title", "old evidence")
    version_id = document["latest_version"]["id"]
    if lifecycle != "delete_space":
        service.add_version(alice, document["id"], "new evidence")
    goal = service.create_goal(owner, "Research goal")
    link = service.link_goal_knowledge(alice, goal["id"], version_id)
    entry = service.add_goal_update(alice, goal["id"], "Bound progress", version_id)
    service.add_goal_update(bob, goal["id"], "Public progress")
    assert service.get_goal(bob, goal["id"])["knowledge"][0]["title"] == "Evidence title"
    if lifecycle == "withdraw_version":
        service.withdraw_version(alice, version_id, "Evidence unreliable")
    elif lifecycle == "delete_version":
        denied(409, service.delete_version, alice, version_id)
        service.withdraw_version(alice, version_id, "Linked version cannot be hard-deleted")
    elif lifecycle == "withdraw_document":
        service.withdraw_document(alice, document["id"])
    else:
        service.delete_space(owner, space["id"])
    for actor in (owner, alice, bob):
        detail = service.get_goal(actor, goal["id"])
        assert detail["knowledge"] == []
        assert [update["content"] for update in detail["updates"]] == ["Public progress"]
        assert detail["status"] == "not_started" and detail["revision"] == 1
    with service.db.transaction() as connection:
        assert connection.execute("SELECT content FROM versions WHERE id=?", (version_id,)).fetchone()[0] == "old evidence"
        assert connection.execute("SELECT id FROM goal_updates WHERE id=?", (entry["id"],)).fetchone()
        assert connection.execute("SELECT id FROM goal_knowledge WHERE id=?", (link["id"],)).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


@pytest.mark.parametrize("code", [403, 404, 410])
def test_source_visibility_only_hides_explicit_acl_lifecycle_errors(pure_world, code):
    service, repository, uow = pure_world
    goal = service.create_goal("owner", "Goal")
    repository.add("goal_knowledge", {"id": "link", "goal_id": goal["id"], "version_id": "hidden", "linked_by": "alice", "created_at": 1})
    repository.add("goal_updates", {"id": "update", "goal_id": goal["id"], "version_id": "hidden", "author_id": "alice", "content": "Hidden entire row", "created_at": 1})

    def unavailable(*args, **kwargs):
        raise DomainError(code, "Unavailable")

    service._version = unavailable
    detail = service.get_goal("bob", goal["id"])
    assert detail["knowledge"] == [] and detail["updates"] == []


@pytest.mark.parametrize("error", [DomainError(401, "Auth failed"), DomainError(409, "Conflict"), DomainError(500, "Adapter failed"), RuntimeError("Storage failed")])
def test_source_visibility_propagates_unexpected_errors(pure_world, error):
    service, repository, uow = pure_world
    goal = service.create_goal("owner", "Goal")
    repository.add("goal_knowledge", {"id": "link", "goal_id": goal["id"], "version_id": "source", "linked_by": "alice", "created_at": 1})

    def unavailable(*args, **kwargs):
        raise error

    service._version = unavailable
    with pytest.raises(type(error)) as caught:
        service.get_goal("bob", goal["id"])
    assert caught.value is error


def test_archive_reason_and_contribution_history_survive_restart_without_fake_status_history(world):
    service, owner, alice, bob = world
    goal = service.create_goal(owner, "Archive with audit")
    update = service.add_goal_update(alice, goal["id"], "Progress stays readable")
    denied(403, service.archive_goal, alice, goal["id"], 1, "Member cannot archive")
    archived = service.archive_goal(owner, goal["id"], 1, "Scope changed; preserve experiment history")
    assert archived["archived_reason"] == "Scope changed; preserve experiment history"
    restarted = build_research_service(service.db_path)
    detail = restarted.get_goal(bob, goal["id"])
    assert detail["archived_reason"] == archived["archived_reason"] and detail["revision"] == 2
    assert detail["status_history"] == [] and detail["updates"] == [update]
    assert restarted.list_goals(bob) == []
    with restarted.db.transaction() as connection:
        event = connection.execute("SELECT actor_id FROM audit_events WHERE action='goal_archived' AND resource_id=?", (goal["id"],)).fetchone()
        assert event[0] == owner


def test_concurrent_owner_changes_use_node_revision_not_global_tree_revision(world):
    service, owner, alice, bob = world
    goal = service.create_goal(owner, "Concurrent")
    unrelated = service.create_goal(owner, "Unrelated")
    service.update_goal(owner, unrelated["id"], 1, title="Does not stale first goal")

    def change(title):
        try:
            return service.update_goal(owner, goal["id"], 1, title=title)
        except DomainError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(change, ["First writer", "Second writer"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count(409) == 1
    assert service.get_goal(owner, goal["id"])["revision"] == 2


def test_link_author_can_cleanup_after_source_access_loss_but_owner_cannot_probe_lifecycle(world):
    service, owner, alice, bob = world
    space = shared(world)
    document = service.add_document(alice, space["id"], "Evidence", "first evidence")
    version = document["latest_version"]["id"]
    service.add_version(alice, document["id"], "second evidence")
    goal = service.create_goal(owner, "Goal")
    bobs_link = service.link_goal_knowledge(bob, goal["id"], version)
    service.revoke_space(owner, space["id"], bob)
    assert service.unlink_goal_knowledge(bob, goal["id"], bobs_link["id"]) == {"unlinked": True}
    alices_link = service.link_goal_knowledge(alice, goal["id"], version)
    service.withdraw_version(alice, version, "Evidence withdrawn")
    denied(403, service.unlink_goal_knowledge, owner, goal["id"], alices_link["id"])
    assert service.unlink_goal_knowledge(alice, goal["id"], alices_link["id"]) == {"unlinked": True}


@pytest.mark.parametrize("error", [DomainError(403, "Denied"), DomainError(404, "Missing"), DomainError(410, "Withdrawn")])
def test_owner_unlink_hidden_source_errors_are_uniform_403(pure_world, error):
    service, repository, uow = pure_world
    goal = service.create_goal("owner", "Goal")
    repository.add("goal_knowledge", {"id": "link", "goal_id": goal["id"], "version_id": "source", "linked_by": "alice", "created_at": 1})

    def unavailable(*args, **kwargs):
        raise error

    service._version = unavailable
    denied(403, service.unlink_goal_knowledge, "owner", goal["id"], "link")
    assert repository.get("goal_knowledge", "link") is not None


def test_goal_authentication_and_disabled_accounts(pure_world):
    service, repository, uow = pure_world
    goal = service.create_goal("owner", "Goal")
    repository.update("users", "alice", {"disabled_at": 100})
    for actor in (None, {"id": "owner", "role": "owner"}, "missing", "alice"):
        denied(401, service.list_goals, actor)
        denied(401, service.get_goal, actor, goal["id"])
        denied(401, service.add_goal_update, actor, goal["id"], "No bypass")
    denied(404, service.get_goal, "bob", "missing")
