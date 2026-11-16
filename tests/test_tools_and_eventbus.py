"""Research tool-boundary tests; no legacy library tools or real-time bus is implied."""
from types import SimpleNamespace

import pytest

from app.application.agents.tools import describe_agents


class Request:
    def __init__(self):
        self.calls = []

    async def search(self, query):
        self.calls.append(("search", query))
        return [{"version_id": "v1", "space_id": "space-1", "content": "研究使用召回率评价。"}]

    async def read_version(self, version_id):
        self.calls.append(("read_version", version_id))
        return {"version_id": version_id}

    async def list_tasks(self):
        self.calls.append(("list_tasks",))
        return [{"id": "task-1", "status": "in_progress"}]

    async def read_evidence(self):
        self.calls.append(("read_evidence",))
        return {"evidence": [], "tasks": []}


def test_research_agents_have_separate_explicit_read_only_tool_sets():
    agents = describe_agents(Request())
    expected = {
        "ReadOnlyRetrievalAgent": {"search_documents", "read_version"},
        "CollaborationAnalysisAgent": {"search_sources", "read_source_version"},
        "ProgressAgent": {"list_tasks"},
        "ResearchCoordinator": {"read_evidence"},
    }
    assert set(agents) == set(expected)
    for name, agent in agents.items():
        assert {tool.name for tool in agent.tools} == expected[name]
        assert all(not any(action in tool.name for action in ("approve", "publish", "delete", "update", "grant")) for tool in agent.tools)


@pytest.mark.asyncio
async def test_research_tool_descriptions_delegate_to_authorized_request_methods():
    request = Request()
    agents = describe_agents(request)
    search = agents["ReadOnlyRetrievalAgent"].tools[0]
    assert (await search.invoke("评价指标"))[0]["space_id"] == "space-1"
    tasks = agents["ProgressAgent"].tools[0]
    assert (await tasks.invoke())[0]["status"] == "in_progress"
    evidence = agents["ResearchCoordinator"].tools[0]
    assert await evidence.invoke() == {"evidence": [], "tasks": []}
    assert request.calls == [("search", "评价指标"), ("list_tasks",), ("read_evidence",)]


def test_coordinator_can_be_constructed_without_any_write_capability():
    # Building descriptions must not require or acquire service write methods.
    async def read():
        return {"evidence": [], "tasks": []}
    request = SimpleNamespace(search=read, read_version=read, list_tasks=read, read_evidence=read)
    assert [tool.name for tool in describe_agents(request)["ResearchCoordinator"].tools] == ["read_evidence"]
