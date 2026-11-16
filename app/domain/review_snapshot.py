"""Frozen review evidence, independent of later source permissions or lifecycle."""
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ReviewSnapshot:
    review_id: str
    version_id: str
    document_id: str
    source_space_id: str
    source_space_kind: str
    submission_mode: str
    title: str
    number: int
    content: str
    filename: str
    source: str
    source_created_by: str
    copied_from: str | None
    lineage_authors: tuple[str, ...]
    created_at: float

    @classmethod
    def capture(cls, review_id: str, version: Mapping[str, Any], space_kind: str,
                authors: set[str], created_at: float) -> "ReviewSnapshot":
        return cls(
            review_id=review_id, version_id=version["id"],
            document_id=version["document_id"], source_space_id=version["space_id"],
            source_space_kind=space_kind, submission_mode="snapshot",
            title=version["title"], number=version["number"], content=version["content"],
            filename=version["filename"], source=version["source"],
            source_created_by=version["created_by"], copied_from=version.get("copied_from"),
            lineage_authors=tuple(sorted(authors | {version["created_by"]})),
            created_at=created_at,
        )

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "ReviewSnapshot":
        values = {key: row[key] for key in cls.__dataclass_fields__}
        values["lineage_authors"] = tuple(values["lineage_authors"])
        return cls(**values)

    def record(self) -> dict:
        return {**self.__dict__, "lineage_authors": list(self.lineage_authors)}

    def receipt(self, goal: Mapping[str, Any] | None = None) -> dict:
        """Safe list metadata; live public goal labels do not alter frozen evidence."""
        return {"title": self.title, "number": self.number,
                "source_space_kind": self.source_space_kind, "mode": self.submission_mode,
                "target_visibility": "results",
                **{key: value for key, value in (goal or {}).items()
                   if key in ("goal_id", "goal_title", "goal_path", "goal_archived")}}

    def public(self) -> dict:
        # Do not expand author profiles, private-space roots, or lineage records.
        return {key: value for key, value in self.record().items() if key != "lineage_authors"}
