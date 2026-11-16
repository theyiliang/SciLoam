"""Bounded, offline text/DOCX imports. Archives are never extracted to disk."""
from __future__ import annotations

import hashlib
import io
import lzma
from pathlib import PurePosixPath
import re
import stat
import zipfile
import zlib
from xml.etree import ElementTree as ET

from app.domain.errors import DomainError
from app.domain.import_ports import (
    MAX_ARCHIVE_PARTS, MAX_IMPORT_BYTES, MAX_UNCOMPRESSED_BYTES, ParsedImport,
)
from app.domain.policies import MAX_CONTENT_LENGTH, text


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _filename(value: str) -> PurePosixPath:
    text(value, "filename", 255)
    if any(char in value for char in ("/", "\\", ":", "\x00")) or any(ord(char) < 32 for char in value):
        raise DomainError(422, "文件名不能包含路径")
    if value in (".", ".."):
        raise DomainError(422, "文件名不正确")
    return PurePosixPath(value)


def _safe_xml(body: bytes) -> str:
    # DOCX producers use UTF-8. Reject other encodings rather than let an XML
    # declaration hide a DTD/entity from the byte-level safety check.
    try:
        value = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DomainError(422, "DOCX 文档必须使用 UTF-8 编码") from exc
    declaration = re.match(r"\s*<\?xml\b[^?]*\?>", value)
    if "\x00" in value or (declaration and re.search(
        r"encoding\s*=\s*['\"](?!utf-8['\"])[^'\"]+", declaration[0], re.I
    )):
        raise DomainError(422, "DOCX 文档必须使用 UTF-8 编码")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", value, re.I):
        raise DomainError(422, "DOCX 文档包含不允许的 DTD/ENTITY 声明")
    return value


def _xml_tree(value: str):
    try:
        depth, count = 0, 0
        parser = ET.iterparse(io.StringIO(value), events=("start", "end"))
        for event, _ in parser:
            if event == "start":
                depth += 1
                count += 1
                if depth > 128 or count > 200_000:
                    raise DomainError(413, "DOCX 文档结构超出安全限制，无法导入")
            else:
                depth -= 1
        return parser.root
    except (ET.ParseError, ValueError, RecursionError) as exc:
        raise DomainError(422, "DOCX 文档内容无法解析") from exc


def _value(element, path, default=None):
    node = element.find(path) if element is not None else None
    return node.get(W + "val", default) if node is not None else default


class TextImportParser:
    """Parse data only: source URLs, XML relationships and scripts are not run."""

    def parse(self, filename: str, body: bytes) -> ParsedImport:
        path = _filename(filename)
        if not isinstance(body, bytes):
            raise DomainError(422, "上传内容格式不正确")
        if len(body) > MAX_IMPORT_BYTES:
            raise DomainError(413, "文件超过 8 MB 上传限制")
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            raise DomainError(415, "暂不支持 PDF 导入，请先转换为 UTF-8 编码的 TXT 或 Markdown")
        if suffix not in (".txt", ".md", ".markdown", ".docx"):
            raise DomainError(415, "不支持的文件格式，请使用 TXT、Markdown 或 DOCX")
        if suffix == ".docx":
            content, positions = self._docx(body)
            output_filename = path.stem + ".md"
            warnings = ["仅导入段落与表格文本；图片、附件、链接和页面排版不会导入，页码位置不可用。"]
        else:
            try:
                content = body.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise DomainError(422, "Text import must be valid UTF-8") from exc
            if "\x00" in content:
                raise DomainError(422, "Text import contains binary NUL characters")
            if len(content) > MAX_CONTENT_LENGTH:
                raise DomainError(413, "Imported text exceeds the 2,000,000 character limit")
            positions, offset = [], 0
            for line_number, line in enumerate(content.splitlines(keepends=True), 1):
                value = line.rstrip("\r\n")
                if value.strip():
                    positions.append({"paragraph": len(positions) + 1, "line": line_number,
                                      "start": offset, "end": offset + len(value)})
                offset += len(line)
            output_filename, warnings = filename, []
        if not content.strip():
            raise DomainError(422, "Imported document contains no text")
        if len(content) > MAX_CONTENT_LENGTH:
            raise DomainError(413, "Imported text exceeds the 2,000,000 character limit")
        return ParsedImport(content, output_filename, {
            "origin_filename": filename, "format": suffix[1:],
            "paragraph_count": len(positions), "positions": positions,
            "parser_version": "sciloam-text-import-v2",
            "input_sha256": hashlib.sha256(body).hexdigest(),
            "text_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "offset_unit": "unicode_codepoint", "interval": "half_open",
            "warnings": warnings, "page_support": False,
        })

    def _docx(self, body: bytes):
        document, structural_parts = None, {}
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_ARCHIVE_PARTS:
                    raise DomainError(413, "DOCX exceeds the 1000 archive part limit")
                if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
                    raise DomainError(413, "DOCX exceeds the 20 MiB uncompressed limit")
                seen, actual_total = set(), 0
                for info in infos:
                    name = info.filename
                    parts = name.rstrip("/").split("/")
                    if (name != info.orig_filename or not name or name.startswith("/") or "\\" in name or ":" in name
                            or "\x00" in name or any(part in ("", ".", "..") for part in parts)
                            or any(ord(char) < 32 for char in name)
                            or name.casefold() in seen
                            or stat.S_ISLNK(info.external_attr >> 16)):
                        raise DomainError(422, "Unsafe or duplicate DOCX archive path")
                    seen.add(name.casefold())
                    if info.flag_bits & 1:
                        raise DomainError(422, "Encrypted DOCX archives are unsupported")
                    if name.lower().endswith((".js", ".vbs", ".exe", ".dll")) or "vbaproject" in name.lower():
                        raise DomainError(422, "Active DOCX archive parts are unsupported")
                    is_xml = name.lower().endswith((".xml", ".rels"))
                    data = bytearray() if is_xml else None
                    actual_part = 0
                    with archive.open(info) as stream:
                        while True:
                            # The bound applies to actual decompressed bytes too,
                            # not just attacker-controlled central directory sizes.
                            chunk = stream.read(min(65536, MAX_UNCOMPRESSED_BYTES - actual_total + 1))
                            if not chunk:
                                break
                            actual_total += len(chunk)
                            actual_part += len(chunk)
                            if actual_total > MAX_UNCOMPRESSED_BYTES:
                                raise DomainError(413, "DOCX exceeds the 20 MiB uncompressed limit")
                            if data is not None:
                                data.extend(chunk)
                    if actual_part != info.file_size:
                        raise DomainError(422, "DOCX archive part size mismatch")
                    if is_xml:
                        value = _safe_xml(bytes(data))
                        if name == "word/document.xml":
                            document = value
                        elif name in ("word/styles.xml", "word/numbering.xml"):
                            structural_parts[name] = _xml_tree(value)
        except DomainError:
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError,
                NotImplementedError, ValueError, EOFError, zlib.error, lzma.LZMAError) as exc:
            raise DomainError(422, "Invalid or unsupported DOCX archive") from exc
        if document is None:
            raise DomainError(422, "DOCX is missing word/document.xml")
        root = _xml_tree(document)
        if root.tag != W + "document" or root.find(W + "body") is None:
            raise DomainError(422, "Invalid DOCX document structure")
        styles_root = structural_parts.get("word/styles.xml")
        styles = {node.get(W + "styleId"): node for node in (() if styles_root is None else styles_root)}
        numbering_root = structural_parts.get("word/numbering.xml")
        abstracts, numberings = {}, {}
        for node in (() if numbering_root is None else numbering_root):
            if node.tag == W + "abstractNum":
                abstracts[node.get(W + "abstractNumId")] = {
                    level.get(W + "ilvl"): level for level in node.findall(W + "lvl")}
            elif node.tag == W + "num":
                numberings[node.get(W + "numId")] = node
        counters = {}
        output, positions, offset = [], [], 0

        def properties(element):
            props = element.find(W + "pPr")
            chain, seen = [props], set()
            style_id = _value(props, W + "pStyle")
            while style_id and style_id not in seen:
                seen.add(style_id)
                if len(seen) > 32:
                    raise DomainError(413, "DOCX style inheritance exceeds safe limits")
                style = styles.get(style_id)
                if style is None:
                    break
                chain.append(style.find(W + "pPr"))
                style_id = _value(style, W + "basedOn")
            return chain

        def first_value(chain, path):
            return next((value for item in chain if (value := _value(item, path)) is not None), None)

        def prefix_for(element):
            chain = properties(element)
            outline = first_value(chain, W + "outlineLvl")
            style = first_value(chain, W + "pStyle") or ""
            heading = re.fullmatch(r"(?:heading|标题)\s*([1-6])", style, re.I)
            if outline is not None and outline.isdigit() and int(outline) < 6:
                level = int(outline) + 1
                return "#" * level + " ", {"kind": "heading", "heading_level": level}
            if heading:
                level = int(heading[1])
                return "#" * level + " ", {"kind": "heading", "heading_level": level}
            num_id = first_value(chain, W + "numPr/" + W + "numId")
            ilvl = first_value(chain, W + "numPr/" + W + "ilvl") or "0"
            if num_id is None or num_id == "0":
                return "", {"kind": "paragraph"}
            if not ilvl.isdigit() or not 0 <= int(ilvl) <= 7:
                raise DomainError(422, "DOCX numbering level is unsupported")
            level_index = int(ilvl)
            numbering = numberings.get(num_id)
            abstract = _value(numbering, W + "abstractNumId")
            level = abstracts.get(abstract, {}).get(ilvl)
            override = next((item for item in numbering.findall(W + "lvlOverride")
                             if item.get(W + "ilvl") == ilvl), None) if numbering is not None else None
            override_level = override.find(W + "lvl") if override is not None else None
            if override_level is not None:
                level = override_level
            fmt = _value(level, W + "numFmt", "decimal")
            start = _value(override, W + "startOverride") or _value(level, W + "start", "1")
            if not re.fullmatch(r"\d{1,9}", start):
                raise DomainError(422, "DOCX numbering start is unsupported")
            key = (num_id, level_index)
            counters[key] = counters.get(key, int(start) - 1) + 1
            for nested in list(counters):
                if nested[0] == num_id and nested[1] > level_index:
                    del counters[nested]
            marker = "-" if fmt == "bullet" else str(counters[key]) + "."
            # A normalized list marker is structural metadata, not source text.
            return "  " * level_index + marker + " ", {
                "kind": "list_item", "list_level": level_index,
                "numbering_format": fmt, "numbering_id": num_id}

        def paragraph(element, location):
            nonlocal offset
            # Only explicit Word text nodes count. Hyperlink targets, embedded
            # instructions and relationship URLs are never interpreted.
            value = "".join(node.text or "" if node.tag == W + "t" else
                            "\t" if node.tag == W + "tab" else
                            "\n" if node.tag in (W + "br", W + "cr") else ""
                            for node in element.iter())
            prefix, metadata = prefix_for(element)
            rendered = prefix + value if value.strip() else value
            output.append(rendered)
            if value.strip():
                positions.append({"paragraph": len(positions) + 1, "start": offset,
                                  "end": offset + len(rendered),
                                  "text_start": offset + len(prefix), "text_end": offset + len(rendered),
                                  **metadata, **location})
            offset += len(rendered) + 2

        def blocks(container, location=None):
            table_index = 0
            for element in container:
                if element.tag == W + "p":
                    paragraph(element, location or {})
                elif element.tag == W + "tbl":
                    table_index += 1
                    for row_index, row in enumerate(element.findall(W + "tr"), 1):
                        for cell_index, cell in enumerate(row.findall(W + "tc"), 1):
                            blocks(cell, {"table": table_index, "row": row_index, "cell": cell_index})
                elif element.tag == W + "sdt":
                    content = element.find(W + "sdtContent")
                    if content is not None:
                        blocks(content, location)

        blocks(root.find(W + "body"))
        return "\n\n".join(output), positions
