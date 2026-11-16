"""Research validation, access-boundary and local retrieval policies."""
import re
from datetime import date
from pathlib import PurePath
from typing import Any

from .errors import DomainError

TOKEN_TTL_SECONDS = 12 * 60 * 60
INVITE_TTL_SECONDS = 48 * 60 * 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_LOCK_SECONDS = 60
MAX_CONTENT_LENGTH = 2_000_000
UNSET = object()


def text(value: Any, name: str, maximum: int = 1000, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise DomainError(422, f"Invalid {name}")
    return value


def email(value: str) -> str:
    value = text(value, "email", 254).strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise DomainError(422, "Invalid email")
    return value


def password(value: str) -> str:
    text(value, "password", 1024)
    if len(value) < 12:
        raise DomainError(422, "Password must contain at least 12 characters")
    return value


def body(content: str, filename: str, source: str) -> None:
    text(content, "content", MAX_CONTENT_LENGTH, empty=True)
    text(filename, "filename", 255, empty=True)
    text(source, "source", 2000, empty=True)
    if filename and PurePath(filename).suffix.lower() not in (".txt", ".md", ".markdown"):
        raise DomainError(422, "Only text and Markdown are supported; parse other formats before adding")


def due_date(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainError(422, "due_date must be ISO YYYY-MM-DD or null")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DomainError(422, "due_date must be ISO YYYY-MM-DD or null") from exc
    if parsed.isoformat() != value:
        raise DomainError(422, "due_date must be ISO YYYY-MM-DD or null")
    return value


def session_token(token, *, invitation=False):
    if not isinstance(token, str) or not token or len(token) > 512:
        raise DomainError(400 if invitation else 401, "Invalid invitation" if invitation else "Invalid session")
    return token


def invitation(row, now):
    if not row or row["expires_at"] <= now:
        raise DomainError(400, "Invalid or expired invitation")
    if row["accepted_by"]:
        raise DomainError(409, "Invitation has already been accepted")
    return row


def login_failure(previous, now):
    count = previous["failures"] + 1 if previous and now - previous["updated_at"] < LOGIN_LOCK_SECONDS else 1
    return {"failures": count, "locked_until": now + LOGIN_LOCK_SECONDS if count >= LOGIN_FAILURE_LIMIT else 0, "updated_at": now}


def share(source, target):
    if source["kind"] == "private" and target["kind"] != "collaboration":
        raise DomainError(403, "Private content can only be explicitly exported by its owner to collaboration")
    if target["id"] == source["id"]:
        raise DomainError(422, "Choose another space")


def review_decision(decision, comment):
    if decision not in ("approve", "reject"):
        raise DomainError(422, "Decision must be approve or reject")
    text(comment, "review comment", 10000, empty=True)
    if decision == "reject" and not comment.strip():
        raise DomainError(422, "Rejection requires a reason")


def citation_ids(citations):
    if citations is None:
        return []
    if not isinstance(citations, list) or len(citations) > 50:
        raise DomainError(422, "Invalid citations")
    result = []
    for citation in citations:
        version_id = citation if isinstance(citation, str) else citation.get("version_id") if isinstance(citation, dict) else None
        if not isinstance(version_id, str):
            raise DomainError(422, "Each citation requires a version_id")
        result.append(version_id)
    return result


def require_citation_space(version, conversation, *, reading=False):
    if version["space_id"] != conversation["space_id"]:
        raise DomainError(403, "Citation is outside this conversation space" if reading else "Citations must belong to this conversation space")


def tokens(value: str) -> set[str]:
    result = set(re.findall(r"[a-z0-9]+(?:[_'-][a-z0-9]+)*", value.casefold()))
    for run in re.findall(r"[㐀-鿿]+", value):
        if len(run) == 1:
            result.add(run)
        else:
            result.update(run[index:index + 2] for index in range(len(run) - 1))
    return result


def rank_evidence(rows, space_id, query, limit):
    query_tokens = tokens(query)
    if not query_tokens:
        return []
    ranked = []
    for row in rows:
        item = dict(row)
        content, title = item["content"], item["title"]
        body_hits, title_hits = query_tokens & tokens(content), query_tokens & tokens(title)
        if not body_hits | title_hits:
            continue
        score = (len(body_hits) + 4 * len(title_hits)) / len(query_tokens)
        if query.strip().casefold() in title.casefold():
            score += 2
        if query.strip().casefold() in content.casefold():
            score += 1
        offsets = []
        for term in body_hits:
            match = re.search(re.escape(term), content, flags=re.IGNORECASE)
            if match:
                offsets.append(match.start())
        offsets.sort()
        left, best_left, best_right = 0, 0, -1
        for right, offset in enumerate(offsets):
            while offset - offsets[left] > 1100:
                left += 1
            if right - left > best_right - best_left:
                best_left, best_right = left, right
        anchor = offsets[best_left] if offsets else 0
        start = max(0, anchor - 200)
        item["content"] = content[start:start + 1600]
        item["snippet"] = content[max(0, anchor - 80):max(0, anchor - 80) + 400]
        item["space_id"] = space_id
        item["score"] = round(score, 6)
        ranked.append(item)
    ranked.sort(key=lambda item: (-item["score"], item["version_id"]))
    return ranked[:limit]
