"""Offline import data boundaries and authorization; no downloaded dependencies."""
import io
import zipfile

import pytest

from app.application.usecases.knowledge_import import KnowledgeImport
from app.domain.errors import DomainError
from app.domain.import_ports import MAX_IMPORT_BYTES
from app.infrastructure.documents import text_import
from app.infrastructure.documents.text_import import TextImportParser
from tests.test_research_service import denied, space_of, world


XML = ('<?xml version="1.0" encoding="UTF-8"?>'
       '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
       '<w:body><w:p><w:r><w:t>First paragraph 中文</w:t></w:r></w:p>'
       '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell one</w:t></w:r></w:p></w:tc>'
       '<w:tc><w:p><w:r><w:t>Cell two</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
       '<w:p><w:r><w:t>Last paragraph</w:t></w:r></w:p></w:body></w:document>')


def docx(xml=XML, extra=()):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
        for name, content in extra:
            archive.writestr(name, content)
    return stream.getvalue()


@pytest.fixture
def parser():
    return TextImportParser()


@pytest.mark.parametrize("filename", ["note.txt", "note.md", "note.markdown", "笔记.MD"])
def test_utf8_text_and_positions(parser, filename):
    body = "# Heading\n\n中文 paragraph\n"
    result = parser.parse(filename, b"\xef\xbb\xbf" + body.encode())
    assert result.content == body
    assert result.filename == filename
    assert result.report["origin_filename"] == filename
    assert result.report["paragraph_count"] == 2
    assert result.report["page_support"] is False
    for position in result.report["positions"]:
        assert body[position["start"]:position["end"]].strip()
        assert "page" not in position


def test_docx_paragraphs_tables_and_character_offsets(parser):
    result = parser.parse("证据.docx", docx())
    assert result.filename == "证据.md"
    assert result.content == "First paragraph 中文\n\nCell one\n\nCell two\n\nLast paragraph"
    assert result.report["paragraph_count"] == 4
    assert result.report["positions"][1]["table"] == 1
    assert result.report["positions"][2]["cell"] == 2
    for position in result.report["positions"]:
        assert result.content[position["start"]:position["end"]] in ("First paragraph 中文", "Cell one", "Cell two", "Last paragraph")
    assert result.report["warnings"]
    assert not any("page" in position for position in result.report["positions"])


def test_docx_styles_numbering_and_table_mapping_are_preserved(parser):
    namespace = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    xml = f'''<w:document {namespace}><w:body>
      <w:p><w:pPr><w:pStyle w:val="ProjectHeading"/></w:pPr><w:r><w:t>研究目标</w:t></w:r></w:p>
      <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="7"/></w:numPr></w:pPr><w:r><w:t>形成研究报告</w:t></w:r></w:p>
      <w:p><w:pPr><w:numPr><w:ilvl w:val="1"/><w:numId w:val="7"/></w:numPr></w:pPr><w:r><w:t>不得使用未授权数据</w:t></w:r></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>2027年6月30日前</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    </w:body></w:document>'''
    styles = f'''<w:styles {namespace}><w:style w:styleId="ProjectHeading"><w:basedOn w:val="BaseHeading"/></w:style>
      <w:style w:styleId="BaseHeading"><w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style></w:styles>'''
    numbering = f'''<w:numbering {namespace}><w:abstractNum w:abstractNumId="2">
      <w:lvl w:ilvl="0"><w:start w:val="3"/><w:numFmt w:val="chineseCounting"/></w:lvl>
      <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="bullet"/></w:lvl>
      </w:abstractNum><w:num w:numId="7"><w:abstractNumId w:val="2"/></w:num></w:numbering>'''
    result = parser.parse("计划.docx", docx(xml, [("word/styles.xml", styles), ("word/numbering.xml", numbering)]))
    assert result.content == "# 研究目标\n\n3. 形成研究报告\n\n  - 不得使用未授权数据\n\n2027年6月30日前"
    positions = result.report["positions"]
    assert positions[0]["heading_level"] == 1
    assert positions[1]["numbering_format"] == "chineseCounting"
    assert positions[2]["list_level"] == 1
    assert positions[3]["table"] == positions[3]["row"] == positions[3]["cell"] == 1
    assert [result.content[p["text_start"]:p["text_end"]] for p in positions] == [
        "研究目标", "形成研究报告", "不得使用未授权数据", "2027年6月30日前"]


def test_structural_xml_uses_same_safety_limits(parser):
    denied(413, parser.parse, "note.docx", docx(extra=[("word/styles.xml", "<a>" * 129 + "</a>" * 129)]))
    denied(422, parser.parse, "note.docx", docx(extra=[("word/numbering.xml", "<unclosed>")]))


@pytest.mark.parametrize("filename,body,status", [
    ("bad.txt", b"\xff", 422), ("empty.md", b" \n\t", 422), ("binary.txt", b"a\x00b", 422),
    ("evidence.pdf", b"%PDF-1.7", 415), ("evidence.html", b"hello", 415),
    ("archive.docx", b"not a zip", 422), ("../note.txt", b"hello", 422),
    ("C:\\note.txt", b"hello", 422), ("/note.txt", b"hello", 422),
])
def test_invalid_inputs_have_clear_errors(parser, filename, body, status):
    with pytest.raises(DomainError) as caught:
        parser.parse(filename, body)
    assert caught.value.status_code == status
    if filename.endswith(".pdf"):
        assert "转换" in caught.value.message and "UTF-8" in caught.value.message


def test_input_and_text_output_limits(parser):
    denied(413, parser.parse, "note.txt", b"x" * (MAX_IMPORT_BYTES + 1))
    denied(413, parser.parse, "note.txt", b"x" * 2_000_001)
    denied(422, parser.parse, "note.txt", "not bytes")


@pytest.mark.parametrize("name", ["../payload.txt", "/absolute.xml", "word/../evil.xml", "word\\evil.xml", "C:/evil.xml", "word//evil.xml", "word/./evil.xml", "word/evil.js", "word/vbaProject.bin"])
def test_archive_paths_and_active_parts_are_rejected_without_extraction(parser, name):
    # ZipInfo normalizes Windows backslashes on construction, so patch the
    # stored central/local name bytes to retain the hostile original spelling.
    safe_name = name.replace("\\", "/")
    archive = docx(extra=[(safe_name, b"inert text")])
    if safe_name != name:
        archive = archive.replace(safe_name.encode(), name.encode())
    denied(422, parser.parse, "note.docx", archive)


@pytest.mark.parametrize("xml", [
    '<!DOCTYPE document [<!ENTITY data "text">]>' + XML,
    '<!ENTITY data SYSTEM "file:///not-read">' + XML,
    '<?xml version="1.0" encoding="UTF-16"?>' + XML,
    XML.encode("utf-16"), XML.replace("</w:body>", ""),
])
def test_xml_entities_dtd_encoding_and_malformed_document_rejected(parser, xml):
    denied(422, parser.parse, "note.docx", docx(xml))


def test_dtd_in_relationship_part_is_rejected_but_url_is_never_fetched(parser):
    denied(422, parser.parse, "note.docx", docx(extra=[("word/_rels/document.xml.rels", '<!DOCTYPE x SYSTEM "https://example.invalid/">')]))
    inert = '<Relationships><Relationship Target="https://example.invalid/not-fetched" TargetMode="External"/></Relationships>'
    assert parser.parse("note.docx", docx(extra=[("word/_rels/document.xml.rels", inert)])).content.startswith("First")


def test_duplicate_and_symlink_parts_are_rejected(parser):
    with pytest.warns(UserWarning, match="Duplicate name"):
        duplicate = docx(extra=[("word/document.xml", XML)])
    denied(422, parser.parse, "note.docx", duplicate)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("word/document.xml", XML)
        link = zipfile.ZipInfo("word/link")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "outside")
    denied(422, parser.parse, "note.docx", stream.getvalue())


def test_missing_document_empty_docx_and_xml_depth_limits(parser):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("other.xml", "<x/>")
    denied(422, parser.parse, "note.docx", stream.getvalue())
    denied(422, parser.parse, "note.docx", docx(XML.replace("<w:t>", "<w:delText>").replace("</w:t>", "</w:delText>")))
    denied(413, parser.parse, "note.docx", docx("<a>" * 129 + "</a>" * 129))


def test_zip_metadata_uncompressed_and_part_limits(parser, monkeypatch):
    monkeypatch.setattr(text_import, "MAX_UNCOMPRESSED_BYTES", 1000)
    denied(413, parser.parse, "note.docx", docx(extra=[("large.bin", b"0" * 1001)]))
    monkeypatch.setattr(text_import, "MAX_ARCHIVE_PARTS", 2)
    denied(413, parser.parse, "note.docx", docx(extra=[("a", b""), ("b", b"")]))


def test_actual_decompressed_bytes_are_bounded_even_if_metadata_lies(parser, monkeypatch):
    class Archive:
        def __init__(self, *_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def infolist(self):
            item = zipfile.ZipInfo("word/document.xml")
            item.file_size = 1
            return [item]

        def open(self, _):
            return io.BytesIO(b"x" * 101)

    monkeypatch.setattr(text_import, "MAX_UNCOMPRESSED_BYTES", 100)
    monkeypatch.setattr(text_import.zipfile, "ZipFile", Archive)
    denied(413, parser.parse, "note.docx", b"zip test double")


def test_import_saves_docx_as_md_with_inert_source_and_persistent_report(world, parser):
    service, owner, alice, bob = world
    private = space_of(service, alice, "private")
    result = KnowledgeImport(service, parser).import_content(alice, private["id"], "来源.docx", docx(), source="https://example.invalid/not-fetched")
    assert result["title"] == "来源"
    assert result["latest_version"]["filename"] == "来源.md"
    assert result["latest_version"]["source"] == "https://example.invalid/not-fetched\nImported from DOCX: 来源.docx"
    assert result["import_report"]["origin_filename"] == "来源.docx"
    saved = service.list_documents(alice, private["id"])
    assert len(saved) == 1 and "import_report" not in saved[0]
    version = service.get_version(alice, result["latest_version"]["id"])
    assert version["import_report"] == result["import_report"]
    assert version["structure_map"]["blocks"] == result["import_report"]["positions"]
    assert version["import_report"]["parser_version"] == "sciloam-text-import-v2"
    assert len(version["import_report"]["input_sha256"]) == 64
    denied(403, service.get_version, owner, version["id"])


def test_parse_report_failure_rolls_back_document_and_version(world, parser, monkeypatch):
    from app.infrastructure.persistence.repositories import SQLiteResearchRepository
    service, owner, alice, bob = world
    private = space_of(service, alice, "private")
    original = SQLiteResearchRepository.add
    def fail_report(repository, entity, record):
        if entity == "knowledge_imports":
            raise RuntimeError("report persistence unavailable")
        return original(repository, entity, record)
    monkeypatch.setattr(SQLiteResearchRepository, "add", fail_report)
    with pytest.raises(RuntimeError, match="report persistence"):
        KnowledgeImport(service, parser).import_content(alice, private["id"], "note.md", b"# Goal")
    assert service.list_documents(alice, private["id"]) == []


def test_preauth_write_role_before_parser_and_invalid_content(world):
    service, owner, alice, bob = world
    private = space_of(service, alice, "private")
    service.grant_space(alice, private["id"], bob, "reader")

    class MustNotParse:
        def parse(self, *_):
            pytest.fail("Unauthorized input reached parser")

    importer = KnowledgeImport(service, MustNotParse())
    for actor, space_id, code in (("missing", private["id"], 401), (owner, private["id"], 403), (bob, private["id"], 403), (alice, "missing", 403)):
        denied(code, importer.import_content, actor, space_id, "bad.pdf", b"invalid")
    assert service.list_documents(alice, private["id"]) == []


def test_revoked_during_parse_final_save_reauthorizes_and_has_no_partial_document(world, parser):
    service, owner, alice, bob = world
    private = space_of(service, alice, "private")
    service.grant_space(alice, private["id"], bob, "editor")

    class RevokeParser:
        def parse(self, filename, body):
            result = parser.parse(filename, body)
            service.revoke_space(alice, private["id"], bob)
            return result

    denied(403, KnowledgeImport(service, RevokeParser()).import_content, bob, private["id"], "note.md", b"text")
    assert service.list_documents(alice, private["id"]) == []


def test_parse_and_validation_failures_have_no_partial_save(world, parser):
    service, owner, alice, bob = world
    private = space_of(service, alice, "private")
    importer = KnowledgeImport(service, parser)
    for filename, body, code in (("bad.docx", b"bad", 422), ("empty.txt", b"", 422), ("bad.pdf", b"PDF", 415)):
        denied(code, importer.import_content, alice, private["id"], filename, body)
    denied(422, importer.import_content, alice, private["id"], "note.docx", docx(), source="s" * 2000)
    denied(413, importer.import_content, alice, private["id"], "note.md", b"x" * (MAX_IMPORT_BYTES + 1))
    assert service.list_documents(alice, private["id"]) == []
