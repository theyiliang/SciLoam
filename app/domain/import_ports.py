"""Pure, synchronous file-to-text import contracts; no file or URL execution."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


MAX_IMPORT_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_PARTS = 1000


@dataclass(frozen=True)
class ParsedImport:
    content: str
    filename: str
    report: dict


class ImportParser(Protocol):
    def parse(self, filename: str, body: bytes) -> ParsedImport: ...
