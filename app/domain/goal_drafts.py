"""Local extractive goal drafts: literal provenance, never model-generated plans."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Mapping

from . import policies
from .errors import DomainError
from .goals import GoalTree, goal_parent, goal_position
from .models import Space, User, Version

MAX_SOURCES = 5
MAX_SOURCE_CHARACTERS = 120000
MAX_DRAFT_NODES = 30
METHOD = "extractive"
LIMITATIONS = (
    "本地确定性原文提取，不是 AI 生成；有明确目标章节时限定章节范围，否则仅识别标题和编号列表，不保证目标完整或语义正确。",
    "仅使用负责人选定的当前有效 basis 课题知识；引文位置按 Python Unicode 字符的左闭右开区间记录。",
    "不推断截止日期、验收标准或缺失步骤；请补充知识并由负责人逐项编辑、确认。",
    "explicit/suggested 表示原文明确要求/待核实建议；rule/ai/manual 独立记录生成方式。",
    "交付物、时间和约束仅从原文提取；未给出的考核指标保持为空。",
)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def tree_hash(records: list[Mapping[str, Any]]) -> str:
    """Include every column and archived row, not just visible goal labels."""
    GoalTree(records).ordered(include_archived=True)
    return canonical_hash(sorted((dict(row) for row in records), key=lambda row: row["id"]))


def version_ids(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_SOURCES:
        raise DomainError(422, "Select one to five current basis knowledge versions")
    for item in value:
        policies.text(item, "knowledge version id", 128)
    if len(set(value)) != len(value):
        raise DomainError(422, "Knowledge version ids must be unique")
    return list(value)


def require_author(user: User, draft: Mapping[str, Any]) -> None:
    user.require_owner()
    if draft["created_by"] != user.id:
        raise DomainError(403, "Only the draft author and project owner may access this draft")


def require_revision(draft: Mapping[str, Any], expected_revision: Any) -> None:
    if type(expected_revision) is not int or expected_revision < 1:
        raise DomainError(422, "expected_revision must be a positive integer")
    if draft["revision"] != expected_revision:
        raise DomainError(409, "Goal draft revision is stale; reload the draft")
    if draft["status"] != "draft" or draft.get('abandoned_at') is not None:
        raise DomainError(409, "Published goal drafts cannot be edited")


def require_confirmation(confirmed: Any) -> None:
    if confirmed is not True:
        raise DomainError(422, "The project owner must explicitly confirm publication")


def validate_sources(user: User, contexts: list[Mapping[str, Any]], *, expected=None) -> list[dict]:
    """Recheck authority, active heads and each immutable version in the domain.

    Contexts contain live port-loaded records, not client assertions. Comparing the
    full immutable version hash also detects illegal in-place version mutation.
    """
    user.require_owner()
    version_ids([context["version"]["id"] for context in contexts])
    sources, total = [], 0
    for context in contexts:
        version, space = context["version"], Space.from_record(context["space"])
        try:
            space.require_access(user, context["membership"])
        except DomainError:
            if expected is not None:
                raise DomainError(409, "Goal draft source permissions changed; generate a new draft") from None
            raise
        if space.kind != "basis" or version["space_id"] != space.id:
            raise DomainError(409 if expected is not None else 403,
                              "Goal draft sources must belong to basis knowledge only")
        if (context["withdrawn"] or context["deleted"] or context["space_deleted"]
                or context["current_version_id"] != version["id"]):
            raise DomainError(409, "Goal draft source is no longer the current active version")
        if not isinstance(version["content"], str):
            raise DomainError(422, "Knowledge source must contain plain text")
        total += len(version["content"])
        sources.append({"version_id": version["id"], "document_id": version["document_id"],
                        "space_id": space.id, "number": version["number"],
                        "content_hash": hashlib.sha256(version["content"].encode("utf-8")).hexdigest(),
                        "version_hash": canonical_hash(Version.from_record(version).record())})
    if total > MAX_SOURCE_CHARACTERS:
        raise DomainError(422, "Selected source text exceeds 120000 characters; select smaller knowledge sources")
    if expected is not None and sources != expected:
        raise DomainError(409, "Goal draft sources changed; generate a new draft")
    return sources


_HEADING = re.compile(r"^\s{0,3}(#{1,8})[ \t]+(?P<title>\S.*)$")
_NUMBERED = re.compile(r"^(?P<indent>[ \t]*)(?P<number>(?:\d+(?:\.\d+)+(?=[ \t])|\d+(?:\.\d+)*[.)、]|[（(](?:\d+|[一二三四五六七八九十百]+)[）)]|[一二三四五六七八九十百]+、))[ \t]*(?P<title>\S.*)$")
_BULLET = re.compile(r"^(?P<indent>[ \t]*)[-*+][ \t]+(?P<title>\S.*)$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
FIELDS = ('title', 'description', 'acceptance_criteria', 'due_date', 'deliverables', 'constraints', 'time_requirement')
_GOAL_SECTION = re.compile(r"^(?:研究总目标|研究目标|总目标|子目标(?:[一二三四五六七八九十百\d]+)?|目标(?:[一二三四五六七八九十百\d]+)?)(?:\s*[：:].*|\s*)$")
_NON_GOAL_SECTION = re.compile(r"^(?:研究背景|背景|研究设计边界|研究边界|讨论中的备选做法|备选做法|备选建议)(?:\s*[：:].*|\s*)$")
_LABELS = re.compile(r"^\s*(?P<label>研究目标|目标|说明|描述|交付物|预期成果|成果|验收标准|考核指标|截止日期|截止时间|时间要求|时间|约束|约束条件)[：:]\s*(?P<value>\S.*)$")
_LABEL_FIELD = {'研究目标': 'description', '目标': 'description', '说明': 'description', '描述': 'description',
                '交付物': 'deliverables', '预期成果': 'deliverables', '成果': 'deliverables',
                '验收标准': 'acceptance_criteria', '考核指标': 'acceptance_criteria',
                '截止日期': 'due_date', '截止时间': 'due_date', '时间要求': 'due_date', '时间': 'due_date',
                '约束': 'constraints', '约束条件': 'constraints'}


def anchor(version_id, content, start, end):
    return {'version_id': version_id, 'quote': content[start:end], 'start': start, 'end': end}


def source_lines(content):
    """Scope literal extraction to goal sections when the document declares them."""
    lines, offset, fence = [], 0, None
    for line in content.splitlines(keepends=True):
        text = line.rstrip('\r\n')
        marker = _FENCE.match(text)
        if marker:
            char = marker.group(1)[0]
            if fence is None:
                fence = (char, len(marker.group(1)))
            elif fence[0] == char and len(marker.group(1)) >= fence[1]:
                fence = None
            heading = None
        else:
            heading = _HEADING.match(text) if fence is None else None
        lines.append((line, offset, heading))
        offset += len(line)
    ancestors, structure = [], set()
    for _, offset, heading in lines:
        if heading:
            level = len(heading.group(1))
            while ancestors and ancestors[-1][0] >= level:
                ancestors.pop()
            if _GOAL_SECTION.fullmatch(heading.group('title').strip()):
                structure.update(start for _, start, title in ancestors if title.endswith('目标'))
            ancestors.append((level, offset, heading.group('title').strip()))
    scoped = any(heading and _GOAL_SECTION.fullmatch(heading.group('title').strip())
                 for _, _, heading in lines)
    sections = []
    for line, offset, heading in lines:
        if heading:
            level = len(heading.group(1))
            while sections and sections[-1][0] >= level:
                sections.pop()
            title = heading.group('title').strip()
            blocked = bool(sections and sections[-1][2]) or bool(_NON_GOAL_SECTION.fullmatch(title))
            active = bool(sections and sections[-1][1]) or bool(_GOAL_SECTION.fullmatch(title))
            sections.append((level, active, blocked))
        active = not scoped or bool(sections and sections[-1][1])
        blocked = bool(sections and sections[-1][2])
        yield line, offset, (active or offset in structure) and not blocked, heading


def enrich_fields(nodes, versions):
    """Literal labelled requirements only; no synthesized metrics or dates."""
    for version in versions:
        current, offset = None, 0
        by_start = {node['source']['start']: node for node in nodes if node['source']['version_id'] == version['id']}
        fence = None
        for line, offset, eligible, heading in source_lines(version['content']):
            if not eligible:
                current = None
                continue
            text = line.rstrip('\r\n')
            marker = _FENCE.match(text)
            if marker:
                char = marker.group(1)[0]
                if fence is None:
                    fence = (char, len(marker.group(1)))
                elif fence[0] == char and len(marker.group(1)) >= fence[1]:
                    fence = None
                offset += len(line)
                continue
            matching = next((node for start, node in by_start.items() if offset <= start < offset + len(line)), None)
            if matching:
                current = matching
            match = _LABELS.match(text) if fence is None and matching is None else None
            if match and current is not None:
                field, value = _LABEL_FIELD[match['label']], match['value'].rstrip()
                start, end = offset + match.start('value'), offset + match.start('value') + len(value)
                if field == 'due_date':
                    date = re.search(r'(?<![\w-])\d{4}-\d{2}-\d{2}(?![\dA-Za-z_-])', value)
                    current['time_requirement'] = value
                    current['field_sources']['time_requirement'] = [anchor(version['id'], version['content'], start, end)]
                    if not date:
                        offset += len(line)
                        continue
                    else:
                        start += date.start()
                        end = start + len(date[0])
                        value = date[0]
                        policies.due_date(value)
                citation = anchor(version['id'], version['content'], start, end)
                if current.get(field) and field != 'due_date':
                    current[field] += '\n' + value
                else:
                    current[field] = value
                current['field_sources'].setdefault(field, []).append(citation)
            offset += len(line)
    return nodes


def extract_nodes(versions: list[Mapping[str, Any]]) -> list[dict]:
    nodes = []
    for version in versions:
        headings, lists, offset, fence = [], [], 0, None
        for line, offset, eligible, section in source_lines(version["content"]):
            if not eligible:
                if section:
                    level = len(section.group(1))
                    while headings and headings[-1][0] >= level:
                        headings.pop()
                    lists = []
                continue
            text = line.rstrip("\r\n")
            marker = _FENCE.match(text)
            if marker:
                char = marker.group(1)[0]
                if fence is None:
                    fence = (char, len(marker.group(1)))
                elif char == fence[0] and len(marker.group(1)) >= fence[1]:
                    fence = None
                offset += len(line)
                continue
            heading = _HEADING.match(text) if fence is None else None
            numbered = (_NUMBERED.match(text) or _BULLET.match(text)) if fence is None and not heading else None
            if heading or numbered:
                match = heading or numbered
                title = match.group("title").rstrip()
                if heading:
                    level = len(heading.group(1))
                    while headings and headings[-1][0] >= level:
                        headings.pop()
                    parent = headings[-1][1] if headings else None
                    lists = []
                else:
                    indent = len(numbered.group("indent").expandtabs(4))
                    # Dotted numbering also conveys hierarchy without indentation.
                    number = numbered.groupdict().get("number", "").rstrip(".)、")
                    level = (indent, number.count("."))
                    while lists and lists[-1][0] >= level:
                        lists.pop()
                    parent = lists[-1][1] if lists else headings[-1][1] if headings else None
                start = offset + match.start("title")
                node_id = "draft-node-" + str(len(nodes) + 1)
                nodes.append({"id": node_id, "parent_id": parent, "title": title,
                              "description": "", "acceptance_criteria": "", "due_date": None,
                              "position": len(nodes), "origin": "explicit",
                              "source": {"version_id": version["id"], "quote": title,
                                         "start": start, "end": start + len(title)}})
                if len(nodes) > MAX_DRAFT_NODES:
                    raise DomainError(422, "Source headings exceed 30 draft nodes; narrow the selected knowledge")
                nodes[-1].update(generation_method='rule', deliverables='', constraints='', time_requirement='',
                                 field_sources={'title': [deepcopy(nodes[-1]['source'])]})
                (headings if heading else lists).append((level, node_id))
            offset += len(line)
    if not nodes:
        raise DomainError(422, "No headings or numbered-list items found; supplement basis knowledge instead of inventing a complete plan")
    return enrich_fields(nodes, versions)


def validate_anchor(value, versions):
    if not isinstance(value, dict) or set(value) != {'version_id', 'quote', 'start', 'end'}:
        raise DomainError(422, 'Source requires exact version_id, quote, start and end')
    policies.text(value['version_id'], 'source version id', 128)
    version = versions.get(value['version_id'])
    start, end, quote = value['start'], value['end'], value['quote']
    if (version is None or type(start) is not int or type(end) is not int
            or not isinstance(quote, str) or not quote
            or not 0 <= start < end <= len(version['content'])
            or version['content'][start:end] != quote):
        raise DomainError(422, 'Source quote must match the exact selected version character range')


def require_publishable_nodes(nodes):
    for node in nodes:
        if node.get('source') is None:
            raise DomainError(422, 'Unsourced manual nodes may remain drafts but cannot be published')
        # Suggestions may paraphrase, but every populated requirement must have
        # explicit field evidence. A title quote cannot justify invented metrics.
        for field in FIELDS:
            if node.get(field) and not node.get('field_sources', {}).get(field):
                raise DomainError(422, f'Publication requires original evidence for {field}')
            if field in ('acceptance_criteria', 'due_date', 'deliverables', 'constraints', 'time_requirement') and node.get(field):
                if node[field] != '\n'.join(c['quote'] for c in node['field_sources'][field]):
                    raise DomainError(422, f'Publication cannot invent or alter original {field} requirements')


def validate_nodes(nodes: Any, versions: list[Mapping[str, Any]], existing_tree: list[dict], *, previous=None) -> list[dict]:
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_DRAFT_NODES:
        raise DomainError(422, "Goal draft must contain one to 30 nodes")
    versions_by_id = {row["id"]: row for row in versions}
    old = {row["id"]: row for row in previous or []}
    existing_ids = {row["id"] for row in existing_tree}
    validated, seen = [], set()
    allowed = {"id", "parent_id", "title", "description", "acceptance_criteria", "due_date", "position", "origin", "source", "field_sources", "generation_method", "deliverables", "constraints", "time_requirement"}
    for value in nodes:
        if not isinstance(value, dict) or set(value) - allowed:
            raise DomainError(422, "Invalid goal draft node fields")
        node = {"parent_id": None, "description": "", "acceptance_criteria": "", "due_date": None,
                "position": 0, "source": None, "deliverables": "", "constraints": "", "time_requirement": "",
                "field_sources": {}, "generation_method": "manual" if previous is not None else "rule", **deepcopy(value)}
        node_id = policies.text(node.get("id"), "draft node id", 128)
        if node_id in seen or node_id in existing_ids:
            raise DomainError(422, "Draft node ids must be unique and cannot overwrite existing goals")
        seen.add(node_id)
        policies.text(node.get("title"), "goal title", 500)
        for field in ("description", "acceptance_criteria", "deliverables", "constraints", "time_requirement"):
            policies.text(node[field], f"goal {field}", 20000, empty=True)
        node["due_date"] = policies.due_date(node["due_date"])
        goal_parent(node["parent_id"])
        goal_position(node["position"])
        if node.get("origin") not in ("explicit", "suggested"):
            raise DomainError(422, "Goal draft origin must be explicit or suggested")
        source = node["source"]
        if source is not None:
            if not isinstance(source, dict) or set(source) != {"version_id", "quote", "start", "end"}:
                raise DomainError(422, "Source requires exact version_id, quote, start and end")
            policies.text(source["version_id"], "source version id", 128)
            version = versions_by_id.get(source["version_id"])
            start, end, quote = source["start"], source["end"], source["quote"]
            if (version is None or type(start) is not int or type(end) is not int
                    or not isinstance(quote, str) or not quote or not 0 <= start < end <= len(version["content"])
                    or version["content"][start:end] != quote):
                raise DomainError(422, "Source quote must match the exact selected version character range")
        field_sources = node['field_sources']
        if not isinstance(field_sources, dict) or set(field_sources) - set(FIELDS):
            raise DomainError(422, 'Invalid field-level source fields')
        if not field_sources and source is not None:
            field_sources['title'] = [deepcopy(source)]
        for field, citations in field_sources.items():
            if not isinstance(citations, list) or len(citations) > 30:
                raise DomainError(422, 'Field sources must be a bounded list of exact anchors')
            for citation in citations:
                validate_anchor(citation, versions_by_id)
        if node['generation_method'] not in ('rule', 'ai', 'manual'):
            raise DomainError(422, 'Invalid generation method')
        if node["origin"] == "explicit":
            for field in FIELDS:
                value = node.get(field)
                if value and value != '\n'.join(c['quote'] for c in field_sources.get(field, [])):
                    raise DomainError(422, 'Explicit fields must be literal source requirements without invented dates or criteria')
            if source is None:
                raise DomainError(422, 'Explicit goals require an exact source anchor')
        if previous is not None:
            prior = old.get(node_id)
            if prior is None:
                node['generation_method'] = 'manual'
                if node['origin'] != 'suggested':
                    raise DomainError(422, 'User-added goals must be marked suggested, not extracted')
            if prior is not None:
                if source != prior["source"]:
                    raise DomainError(422, "Editing a draft must preserve the original source quote")
                if any(node.get(key) != prior.get(key) for key in FIELDS):
                    node['generation_method'] = 'manual'
                elif node['generation_method'] != prior.get('generation_method', 'rule'):
                    node['generation_method'] = prior.get('generation_method', 'rule')
        validated.append(node)
    # Validate the complete detached real tree plus temporary draft IDs. Never
    # replace existing rows or rely on a separate, weaker draft-only tree rule.
    combined = list(existing_tree) + [{**node, "status": "not_started", "revision": 1,
                                      "archived_at": None, "created_at": 0} for node in validated]
    tree = GoalTree(combined)
    for node in validated:
        if node["parent_id"] not in seen and node["parent_id"] not in existing_ids and node["parent_id"] is not None:
            raise DomainError(422, "Draft parent must reference a draft or existing goal")
        tree.require_placement(node["id"], node["parent_id"])
    ordered = tree.ordered(include_archived=True)
    return [next(node for node in validated if node["id"] == row["id"]) for row in ordered if row["id"] in seen]
