"""Explicit offline uploads, separate from existing document/idempotency APIs."""
from pathlib import PurePosixPath

from app.domain import policies
from app.domain.errors import DomainError
from app.domain.import_ports import MAX_IMPORT_BYTES, ImportParser


class KnowledgeImport:
    def __init__(self, service, parser: ImportParser):
        self.service = service
        self.parser = parser

    def import_content(self, actor, space_id, filename, body: bytes, title="", source=""):
        # Authentication/write authorization precedes expensive parser work and
        # format errors. The service remains the authority, including at save time.
        self.service.get_me(actor)
        space = next((row for row in self.service.list_spaces(actor) if row["id"] == space_id), None)
        if space is None or space.get("role") not in ("owner", "editor"):
            raise DomainError(403, "Write access to the import space is required")
        policies.text(filename, "filename", 255)
        policies.text(title, "document title", 500, empty=True)
        policies.text(source, "source", 2000, empty=True)
        if not isinstance(body, bytes):
            raise DomainError(422, "Import body must be bytes")
        if len(body) > MAX_IMPORT_BYTES:
            raise DomainError(413, "Import exceeds the 8 MiB input limit")
        parsed = self.parser.parse(filename, body)
        # The source is inert provenance text, never a URL to fetch. DOCX keeps its
        # original name here/report while using the existing Markdown storage policy.
        if PurePosixPath(filename).suffix.lower() == ".docx":
            marker = "Imported from DOCX: " + filename
            source = source + "\n" + marker if source else marker
        policies.text(source, "source", 2000, empty=True)
        document = self.service.add_document(
            actor, space_id, title.strip() or PurePosixPath(filename).stem,
            parsed.content, parsed.filename, source,
            import_report=parsed.report,
            structure_map={"blocks": parsed.report["positions"],
                           "offset_unit": "unicode_codepoint", "interval": "half_open"},
        )
        return {**document, "import_report": parsed.report}
