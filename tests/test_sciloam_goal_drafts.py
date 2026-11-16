"""Local-only goal draft provenance, limits, authorization and atomic append tests."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.application.usecases.common import ResearchContext
from app.application.usecases.goal_drafts import GoalDraftUseCases
from app.application.usecases.goals import GoalUseCases
from app.domain import goal_drafts as rules
from app.domain.errors import DomainError
from tests.test_research_domain import MemoryRepository, MemoryUow, TestSecurity, denied


class DraftService(GoalDraftUseCases, GoalUseCases, ResearchContext):
    pass


class Repository(MemoryRepository):
    KEYS = {**MemoryRepository.KEYS, "document_heads": "document_id", "version_lifecycle": "version_id",
            "space_deletions": "space_id", "goal_draft_publications": "draft_id", 'goal_generations': 'draft_id',
            'knowledge_imports': 'version_id'}

    def version_details(self, version_id):
        version = self.get("versions", version_id)
        if version is None:
            return None
        document = self.get("documents", version["document_id"])
        return {**version, "space_id": document["space_id"], "title": document["title"]}

    def document_active_head(self, document_id):
        head = self.get("document_heads", document_id)
        if head is None:
            return None
        version = self.version_details(head["version_id"])
        if self.get("version_lifecycle", version["id"]) or self.get("document_withdrawals", document_id):
            return None
        return version


@pytest.fixture
def world():
    repository = Repository()
    uow = MemoryUow(repository)
    service = DraftService(lambda: uow, TestSecurity(), lambda: 123.0)
    for actor, role in (("owner", "owner"), ("member", "member"), ("other", "owner")):
        repository.add("users", {"id": actor, "email": actor + "@example.org", "role": role, "created_at": 1})
    for kind in ("basis", "private", "results", "collaboration"):
        repository.add("spaces", {"id": kind, "kind": kind, "owner_id": "owner", "epoch": 1})
    add_source(repository)
    return service, repository, uow


def add_source(repository, version_id="v1", content="# 研究总目标\n1. 样本收集\n   1. 数据核对\n## 结果评估\n普通描述不提取。", space="basis", document_id=None):
    document_id = document_id or "doc-" + version_id
    if repository.get("documents", document_id) is None:
        repository.add("documents", {"id": document_id, "space_id": space, "title": "课题知识", "created_by": "owner", "created_at": 1})
    repository.add("versions", {"id": version_id, "document_id": document_id, "number": 1,
                   "content": content, "filename": "research.md", "source": "local",
                   "created_by": "owner", "created_at": 1, "copied_from": None})
    repository.delete_where("document_heads", document_id=document_id)
    repository.add("document_heads", {"document_id": document_id, "version_id": version_id})
    return version_id


def test_local_extraction_is_literal_deterministic_and_does_not_touch_tree(world):
    service, repo, _ = world
    first = service.generate_goal_draft("owner", ["v1"])
    second = service.generate_goal_draft("owner", ["v1"])
    assert first["nodes"] == second["nodes"]
    assert first["method"] == "extractive" and any("不是 AI" in value for value in first["limitations"])
    assert [node["title"] for node in first["nodes"]] == ["研究总目标", "样本收集", "数据核对", "结果评估"]
    content = repo.get("versions", "v1")["content"]
    for node in first["nodes"]:
        citation = node["source"]
        assert content[citation["start"]:citation["end"]] == citation["quote"] == node["title"]
        assert node["origin"] == "explicit"
        assert node["acceptance_criteria"] == node["description"] == "" and node["due_date"] is None
    assert first["nodes"][2]["parent_id"] == first["nodes"][1]["id"]
    assert repo.list("goal_nodes") == []
    assert service.get_goal_draft("owner", first["id"]) == first
    assert len(service.list_goal_drafts("owner")) == 2


def test_fenced_code_is_not_interpreted_as_goals_and_offsets_are_unicode(world):
    service, repo, _ = world
    repo.update("versions", "v1", {"content": "说明😀\r\n```\r\n# 伪标题\r\n```\r\n# 真标题  \r\n"})
    draft = service.generate_goal_draft("owner", ["v1"])
    assert len(draft["nodes"]) == 1
    assert draft["nodes"][0]["title"] == "真标题"
    assert draft["nodes"][0]["source"]["start"] == repo.get("versions", "v1")["content"].index("真标题")


def test_dotted_and_chinese_numbered_lists_preserve_literal_titles(world):
    service, repo, _ = world
    repo.update("versions", "v1", {"content": "1. 第一目标\n1.1 子目标\n1.2 第二子目标\n（一）中文目标\n二、另一目标"})
    nodes = service.generate_goal_draft("owner", ["v1"])["nodes"]
    assert [node["title"] for node in nodes] == ["第一目标", "子目标", "第二子目标", "中文目标", "另一目标"]
    assert nodes[1]["parent_id"] == nodes[2]["parent_id"] == nodes[0]["id"]


def test_only_owner_author_and_basis_sources_allowed(world):
    service, repo, _ = world
    draft = service.generate_goal_draft("owner", ["v1"])
    for actor in ("member", "other"):
        denied(403, service.get_goal_draft, actor, draft["id"])
        denied(403, service.update_goal_draft, actor, draft["id"], 1, draft["nodes"])
        denied(403, service.publish_goal_draft, actor, draft["id"], 1, confirmed=True)
    denied(403, service.generate_goal_draft, "member", ["v1"])
    denied(403, service.list_goal_drafts, "member")
    for kind in ("private", "results", "collaboration"):
        version_id = add_source(repo, kind + "-version", "# 不可作为来源", kind)
        denied(403, service.generate_goal_draft, "owner", [version_id])
    repo.update("users", "owner", {"disabled_at": 2})
    denied(401, service.get_goal_draft, "owner", draft["id"])


@pytest.mark.parametrize("ids", [[], ["v1"] * 2, ["v1"] * 6, "v1", [True]])
def test_source_selection_is_bounded_unique_and_typed(world, ids):
    service, _, _ = world
    denied(422, service.generate_goal_draft, "owner", ids)


def test_text_and_node_limits_reject_without_truncating_or_persisting(world):
    service, repo, _ = world
    for content in ("", "普通段落，没有标题或编号。", "# 标题\n" + "字" * 120000,
                    "\n".join(f"{index}. 标题" for index in range(31))):
        repo.update("versions", "v1", {"content": content})
        denied(422, service.generate_goal_draft, "owner", ["v1"])
        assert repo.list("goal_drafts") == []
    repo.update("versions", "v1", {"content": "# 标题\n" + "字" * (120000 - len("# 标题\n"))})
    assert service.generate_goal_draft("owner", ["v1"])["nodes"][0]["title"] == "标题"


def test_combined_text_limit_and_deep_tree_are_not_silently_shortened(world):
    service, repo, _ = world
    repo.update("versions", "v1", {"content": "# 标题\n" + "字" * 60000})
    add_source(repo, "v2", "# 第二标题\n" + "字" * 60000)
    denied(422, service.generate_goal_draft, "owner", ["v1", "v2"])
    repo.update("versions", "v1", {"content": "# 根\n" + "\n".join("  " * index + "1. 子级" for index in range(8))})
    denied(409, service.generate_goal_draft, "owner", ["v1"])
    assert repo.list("goal_drafts") == []


def test_human_edits_preserve_quotes_mark_suggestions_and_advance_revision(world):
    service, _, _ = world
    draft = service.generate_goal_draft("owner", ["v1"])
    nodes = deepcopy(draft["nodes"])
    nodes[0].update(title="负责人编辑的目标", origin="suggested", acceptance_criteria="人工验收标准", due_date="2027-01-01")
    nodes.append({"id": "manual", "title": "人工新增", "origin": "suggested", "parent_id": nodes[0]["id"]})
    updated = service.update_goal_draft("owner", draft["id"], 1, nodes)
    assert updated["revision"] == 2 and updated["limitations"] == draft["limitations"]
    assert updated["nodes"][0]["source"] == draft["nodes"][0]["source"]
    assert next(node for node in updated["nodes"] if node["id"] == "manual")["source"] is None
    denied(409, service.update_goal_draft, "owner", draft["id"], 1, nodes)
    for revision in (True, 0, "2", None):
        denied(422, service.update_goal_draft, "owner", draft["id"], revision, nodes)


@pytest.mark.parametrize("tamper", ["quote", "offset", "version", "drop_source", "explicit_edit", "date", "criteria", "new_explicit"])
def test_provenance_cannot_be_forged_or_discarded(world, tamper):
    service, repo, _ = world
    draft = service.generate_goal_draft("owner", ["v1"])
    nodes = deepcopy(draft["nodes"])
    if tamper == "quote":
        nodes[0]["source"]["quote"] = "假的"
    elif tamper == "offset":
        nodes[0]["source"]["start"] += 1
    elif tamper == "version":
        nodes[0]["source"]["version_id"] = "private"
    elif tamper == "drop_source":
        nodes[0].update(origin="suggested", source=None)
    elif tamper == "explicit_edit":
        nodes[0]["title"] = "偷偷改写"
    elif tamper == "date":
        nodes[0]["due_date"] = "2027-01-01"
    elif tamper == "criteria":
        nodes[0]["acceptance_criteria"] = "伪造验收标准"
    else:
        nodes.append({**deepcopy(nodes[0]), "id": "new-explicit"})
    before = deepcopy(repo.rows)
    denied(422, service.update_goal_draft, "owner", draft["id"], 1, nodes)
    assert repo.rows == before


@pytest.mark.parametrize("change", ["content", "immutable_metadata", "head", "withdrawal", "lifecycle", "space", "space_deleted"])
def test_changed_sources_keep_history_readable_but_block_edit_and_publish_without_writes(world, change):
    service, repo, _ = world
    draft = service.generate_goal_draft("owner", ["v1"])
    if change == "content":
        repo.update("versions", "v1", {"content": "# 原位篡改"})
    elif change == "immutable_metadata":
        repo.update("versions", "v1", {"filename": "changed.md"})
    elif change == "head":
        add_source(repo, "v2", "# 新版本", document_id="doc-v1")
    elif change == "withdrawal":
        repo.add("document_withdrawals", {"document_id": "doc-v1"})
    elif change == "lifecycle":
        repo.add("version_lifecycle", {"version_id": "v1", "status": "withdrawn"})
    elif change == "space":
        repo.update("documents", "doc-v1", {"space_id": "private"})
    else:
        repo.add("space_deletions", {"space_id": "basis"})
    before = deepcopy(repo.rows)
    status = 409
    history = service.get_goal_draft('owner', draft['id'])
    assert not history['publication_readiness']['publishable']
    assert history['publication_readiness']['reasons']
    assert len(service.list_goal_drafts('owner')) == 1
    denied(status, service.update_goal_draft, "owner", draft["id"], 1, draft["nodes"])
    denied(status, service.publish_goal_draft, "owner", draft["id"], 1, confirmed=True)
    assert repo.rows == before


def test_tree_changes_prevent_overwrite_and_full_tree_hash_includes_archived_metadata(world):
    service, repo, _ = world
    goal = service.create_goal("owner", "既有目标")
    draft = service.generate_goal_draft("owner", ["v1"])
    repo.update("goal_nodes", goal["id"], {"description": "基线后变更"})
    before = deepcopy(repo.rows)
    denied(409, service.publish_goal_draft, "owner", draft["id"], 1, confirmed=True)
    denied(409, service.update_goal_draft, "owner", draft["id"], 1, draft["nodes"])
    assert repo.rows == before
    assert rules.tree_hash(repo.list("goal_nodes")) != draft["base_tree_hash"]


def test_publication_requires_literal_confirmation_is_append_only_and_idempotent(world):
    service, repo, _ = world
    existing = service.create_goal("owner", "不可覆盖")
    original_goal = repo.get("goal_nodes", existing["id"])
    draft = service.generate_goal_draft("owner", ["v1"])
    for confirmation in (False, None, "true", 1):
        denied(422, service.publish_goal_draft, "owner", draft["id"], 1, confirmed=confirmation)
    result = service.publish_goal_draft("owner", draft["id"], 1, confirmed=True)
    assert repo.get("goal_nodes", existing["id"]) == original_goal
    assert len(repo.list("goal_nodes")) == 1 + len(draft["nodes"])
    assert len(repo.list("goal_knowledge")) == len(draft["nodes"])
    assert set(result["node_id_mapping"]) == {node["id"] for node in draft["nodes"]}
    assert set(result["goal_ids"]) == set(result["node_id_mapping"].values())
    before = deepcopy(repo.rows)
    assert service.publish_goal_draft("owner", draft["id"], 1, confirmed=True) == result
    assert repo.rows == before
    denied(409, service.update_goal_draft, "owner", draft["id"], 2, draft["nodes"])
    assert repo.get("goal_drafts", draft["id"])["status"] == "published"


def test_publish_rolls_back_every_node_link_and_receipt_if_any_write_fails(world):
    service, repo, uow = world
    draft = service.generate_goal_draft("owner", ["v1"])
    before, original = deepcopy(repo.rows), repo.add
    def fail(entity, record):
        if entity == "goal_draft_publications":
            raise RuntimeError("Injected write failure")
        original(entity, record)
    repo.add = fail
    with pytest.raises(RuntimeError, match="Injected"):
        service.publish_goal_draft("owner", draft["id"], 1, confirmed=True)
    assert repo.rows == before and uow.rollbacks == 1


def test_draft_nodes_use_existing_tree_rules_and_never_replace_real_ids(world):
    service, repo, _ = world
    parent = service.create_goal("owner", "既有根")
    draft = service.generate_goal_draft("owner", ["v1"])
    nodes = deepcopy(draft["nodes"])
    nodes[0]["parent_id"] = parent["id"]
    updated = service.update_goal_draft("owner", draft["id"], 1, nodes)
    result = service.publish_goal_draft("owner", draft["id"], 2, confirmed=True)
    new_root = repo.get("goal_nodes", result["node_id_mapping"][nodes[0]["id"]])
    assert new_root["parent_id"] == parent["id"]
    assert updated["nodes"][0]["source"] == draft["nodes"][0]["source"]
    fresh = service.generate_goal_draft("owner", ["v1"])
    forged = deepcopy(fresh["nodes"])
    forged[0]["id"] = parent["id"]
    denied(422, service.update_goal_draft, "owner", fresh["id"], 1, forged)


@pytest.mark.parametrize("source", [[], "quote", {"version_id": [], "quote": "a", "start": 0, "end": 1},
                                    {"version_id": "v1", "quote": "研究总目标", "start": True, "end": 7}])
def test_untrusted_quote_shapes_are_validation_errors(world, source):
    service, _, _ = world
    draft = service.generate_goal_draft("owner", ["v1"])
    nodes = deepcopy(draft["nodes"])
    nodes[0]["source"] = source
    denied(422, service.update_goal_draft, "owner", draft["id"], 1, nodes)


def test_real_sqlite_concurrent_publish_restart_and_protected_source(tmp_path):
    from app.application.usecases.facade import ResearchService
    from app.infrastructure.persistence.factory import build_research_service
    from app.infrastructure.persistence.unit_of_work import SQLiteResearchUnitOfWork
    from app.infrastructure.security.research_passwords import ResearchPasswordSecurity
    from tests.test_research_service import PASSWORD, space_of

    base = build_research_service(tmp_path / "drafts.db")
    def service():
        return ResearchService(lambda: SQLiteResearchUnitOfWork(base.db), ResearchPasswordSecurity(), lambda: 123.0)
    app = service()
    owner = app.bootstrap("owner@example.org", PASSWORD, "Research")["user"]["id"]
    basis = space_of(app, owner, "basis")
    document = app.add_document(owner, basis["id"], "研究知识", "# 总目标\n1. 收集样本\n2. 验证结果", "research.md")
    version_id = document["latest_version"]["id"]
    existing = app.create_goal(owner, "保留已有目标")
    draft = app.generate_goal_draft(owner, [version_id])
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: service().publish_goal_draft(owner, draft["id"], 1, confirmed=True), range(6)))
    assert all(result == results[0] for result in results)
    assert len(app.list_goals(owner)) == 4
    assert app.get_goal(owner, existing["id"])["title"] == "保留已有目标"
    assert service().publish_goal_draft(owner, draft["id"], 1, confirmed=True) == results[0]
    with base.db.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM goal_draft_publications").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM goal_knowledge").fetchone()[0] == 3
    # A draft's evidence is durable and protects the immutable source version.
    denied(409, app.delete_version, owner, version_id)
