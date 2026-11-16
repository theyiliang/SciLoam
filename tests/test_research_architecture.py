"""Fail-closed dependency checks for the actual, flattened research packages."""
import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAYERS = {
    "app/domain": {"models.py", "policies.py", "ports.py", "ai_ports.py", "engineering_ports.py", "retrieval_ports.py", "errors.py", "goals.py", "goal_drafts.py", "goal_generation.py", "goal_generation_runs.py", "import_ports.py", "run_events.py"},
    "app/application/usecases": {"common.py", "identity.py", "spaces.py", "documents.py", "reviews.py", "tasks.py", "goals.py", "goal_drafts.py", "knowledge_import.py", "goal_generation_runs.py", "chat_runs.py", "conversations.py", "facade.py", "retrieval.py", "request_idempotency.py"},
    "app/application/agents": {"orchestrator.py", "evidence.py", "tools.py", "policies.py"},
    "app/infrastructure/ai": {"factory.py", "agentscope_runtime.py", "model_gateway.py", "goal_generator.py"},
    "app/infrastructure/persistence": {"database.py", "repositories.py", "unit_of_work.py", "factory.py", "research_engineering.py"},
    "app/infrastructure/retrieval": {"factory.py", "settings.py", "embedding.py", "reranker.py", "qdrant_index.py"},
    "app/infrastructure/documents": {"text_import.py"},
    "app/infrastructure/events": {"run_bus.py", "goal_generation_bus.py"},
}


def source_files(directory: Path, required=()) -> list[Path]:
    assert directory.is_dir(), f"Missing architecture target: {directory}"
    paths = sorted(directory.rglob("*.py"))
    assert any(path.name != "__init__.py" for path in paths), f"Empty architecture target: {directory}"
    missing = set(required) - {path.name for path in paths}
    assert not missing, f"Missing required modules in {directory}: {sorted(missing)}"
    return paths


def imports(path: Path, root: Path = ROOT):
    relative = path.relative_to(root).with_suffix("")
    package = ".".join(relative.parts[:-1])
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from (item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                yield resolve_name("." * node.level + (node.module or ""), package)
            elif node.module:
                yield node.module


def within(name, prefix):
    return name == prefix or name.startswith(prefix + ".")


def test_all_architecture_targets_exist_and_cover_current_modules():
    for directory, required in LAYERS.items():
        source_files(ROOT / directory, required)


def test_domain_is_independent_of_outer_layers():
    forbidden = ("sqlite3", "fastapi", "agentscope", "pydantic", "httpx")
    for path in source_files(ROOT / "app/domain", LAYERS["app/domain"]):
        for name in imports(path):
            assert not any(within(name, prefix) for prefix in forbidden), (path, name)
            assert not within(name, "app") or within(name, "app.domain"), (path, name)


def test_application_does_not_depend_on_concrete_adapters():
    forbidden = ("app.infrastructure", "app.presentation", "app.composition", "app.research", "sqlite3", "fastapi", "agentscope", "httpx")
    for directory in ("app/application/usecases", "app/application/agents"):
        for path in source_files(ROOT / directory, LAYERS[directory]):
            for name in imports(path):
                assert not any(within(name, prefix) for prefix in forbidden), (path, name)


def test_active_research_modules_do_not_depend_on_library_or_old_monolith():
    forbidden = ("app.research", "app.application.research", "app.domain.library_catalog", "app.domain.hold", "app.domain.patron", "app.infrastructure.rag.library_knowledge", "app.application.tools.library_search_tool")
    paths = [ROOT / "app/composition.py"] + source_files(ROOT / "app/presentation")
    for directory, required in LAYERS.items():
        paths.extend(source_files(ROOT / directory, required))
    for path in paths:
        assert path.is_file(), path
        for name in imports(path):
            assert not any(within(name, prefix) for prefix in forbidden), (path, name)


def test_http_adapter_has_no_sqlite_or_direct_sql_execution():
    path = ROOT / "app/presentation/server.py"
    assert not any(within(name, "sqlite3") for name in imports(path))
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr in {"execute", "executemany", "executescript"}
                   for node in ast.walk(tree))


def test_architecture_scan_rejects_missing_empty_and_incomplete_targets(tmp_path):
    with pytest.raises(AssertionError, match="Missing architecture target"):
        source_files(tmp_path / "missing")
    with pytest.raises(AssertionError, match="Empty architecture target"):
        source_files(tmp_path)
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    with pytest.raises(AssertionError, match="Empty architecture target"):
        source_files(tmp_path)
    (tmp_path / "other.py").write_text("", encoding="utf-8")
    with pytest.raises(AssertionError, match="Missing required modules"):
        source_files(tmp_path, {"models.py"})


def test_architecture_scan_resolves_relative_outer_layer_imports(tmp_path):
    path = tmp_path / "app/application/usecases/example.py"
    path.parent.mkdir(parents=True)
    path.write_text("from ...infrastructure.persistence import factory\nfrom .common import ResearchContext\n", encoding="utf-8")
    assert set(imports(path, tmp_path)) == {"app.infrastructure.persistence", "app.application.usecases.common"}
