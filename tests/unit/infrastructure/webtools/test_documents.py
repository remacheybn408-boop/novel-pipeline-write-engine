"""Offline tests for fetch_document (PDF/DOCX/XLSX/CSV parsing).

Fixtures are generated in code (pypdf / python-docx / openpyxl / raw bytes) —
no binary fixtures in the repo. Network is mocked throughout.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from proseforge.infrastructure.webtools import BytesOutcome, SafeFetcher
from proseforge.infrastructure.webtools.documents import (
    fetch_document,
    parse_document_bytes,
    sniff_document_kind,
)

# ---------- fixture generators ----------


def make_pdf(text: str, pages: int = 2) -> bytes:
    """Minimal valid text PDF built by hand (pypdf-readable)."""
    objects: list[bytes] = []
    page_ids = [3 + i * 2 for i in range(pages)]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    font_id = 3 + pages * 2
    for index, pid in enumerate(page_ids):
        content_id = pid + 1
        stream = f"BT /F1 12 Tf 72 720 Td ({text} p{index + 1}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_start = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF".encode())
    return out.getvalue()


def make_docx(text: str) -> bytes:
    import docx

    document = docx.Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_xlsx() -> bytes:
    import openpyxl

    workbook = openpyxl.Workbook()
    first = workbook.active
    first.title = "数据"
    first.append(["名称", "数量"])
    first.append(["苹果", 3])
    second = workbook.create_sheet("备注")
    second.append(["说明"])
    second.append(["阶段二测试"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def make_zip_bomb() -> bytes:
    # 2MB of one repeated byte compresses far beyond the 100:1 ratio limit.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"A" * 2_000_000)
    return buffer.getvalue()


class StubFetcher:
    """fetcher seam: returns a preset BytesOutcome without any HTTP."""

    def __init__(self, outcome: BytesOutcome):
        self._outcome = outcome

    async def fetch_bytes(self, url: str, **kwargs) -> BytesOutcome:
        return self._outcome


def stub_for(data: bytes) -> StubFetcher:
    return StubFetcher(BytesOutcome(data, "https://files.example/doc", 200))


# ---------- magic sniffing ----------


def test_sniff_corrects_wrong_extension():
    # URL says .pdf but the body is a real xlsx — magic bytes win.
    assert sniff_document_kind(make_xlsx()) == "xlsx"
    assert sniff_document_kind(make_docx("hi")) == "docx"
    assert sniff_document_kind(make_pdf("hello", pages=1)) == "pdf"
    assert sniff_document_kind(b"a,b,c\n1,2,3\n") == "csv"
    assert sniff_document_kind(b"PK\x03\x04broken") == "corrupted"


# ---------- happy paths ----------


@pytest.mark.asyncio
async def test_pdf_ok():
    outcome = await fetch_document("https://files.example/report.pdf", fetcher=stub_for(make_pdf("Hello ProseForge")))
    assert outcome["status"] == "ok" and outcome["kind"] == "pdf"
    assert outcome["metadata"]["pages"] == 2
    assert "Hello ProseForge" in outcome["text"]


@pytest.mark.asyncio
async def test_docx_ok():
    text = "阶段二 DOCX 正文内容，用于验证解析器能够正常提取这段文字。"
    outcome = await fetch_document("https://files.example/notes.docx", fetcher=stub_for(make_docx(text)))
    assert outcome["status"] == "ok" and outcome["kind"] == "docx"
    assert "阶段二 DOCX 正文内容" in outcome["text"]


@pytest.mark.asyncio
async def test_xlsx_ok():
    outcome = await fetch_document("https://files.example/table.xlsx", fetcher=stub_for(make_xlsx()))
    assert outcome["status"] == "ok" and outcome["kind"] == "xlsx"
    assert outcome["metadata"]["sheets"] == 2
    assert "苹果" in outcome["text"] and "阶段二测试" in outcome["text"]


@pytest.mark.asyncio
async def test_csv_ok_with_bom_and_semicolon():
    data = "名称;数量\n苹果;3\n".encode("utf-8-sig")
    outcome = await fetch_document("https://files.example/list.csv", fetcher=stub_for(data))
    assert outcome["status"] == "ok" and outcome["kind"] == "csv"
    assert outcome["metadata"]["encoding"] == "utf-8-sig"
    assert outcome["metadata"]["delimiter"] == ";"
    assert "苹果" in outcome["text"]


@pytest.mark.asyncio
async def test_magic_overrides_extension():
    # .pdf URL serving an xlsx body parses as xlsx.
    outcome = await fetch_document("https://files.example/wrong.pdf", fetcher=stub_for(make_xlsx()))
    assert outcome["status"] == "ok" and outcome["kind"] == "xlsx"


# ---------- failure honesty ----------


@pytest.mark.asyncio
async def test_corrupted_pdf():
    outcome = await fetch_document("https://files.example/bad.pdf", fetcher=stub_for(b"%PDF-1.4 this is not a real pdf at all"))
    assert outcome["status"] == "corrupted"


@pytest.mark.asyncio
async def test_zip_bomb_preflight_rejects():
    outcome = await fetch_document("https://files.example/bomb.docx", fetcher=stub_for(make_zip_bomb()))
    assert outcome["status"] == "too_large"


@pytest.mark.asyncio
async def test_too_large_status():
    outcome = await fetch_document(
        "https://files.example/huge.pdf",
        fetcher=StubFetcher(BytesOutcome(None, "https://files.example/huge.pdf", 200, "document exceeds 20971520 bytes", "too_large", too_large=True)),
    )
    assert outcome["status"] == "too_large"


@pytest.mark.asyncio
async def test_scanned_pdf_honest_failure():
    # Two BLANK pages: a valid PDF with no extractable text (like a scan).
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    outcome = await fetch_document("https://files.example/scan.pdf", fetcher=stub_for(buffer.getvalue()))
    assert outcome["status"] == "extraction_failed"
    assert outcome["metadata"]["pages"] == 2


@pytest.mark.asyncio
async def test_ssrf_blocked():
    fetcher = SafeFetcher(transport=httpx.MockTransport(lambda request: httpx.Response(200)), rate_seconds_per_domain=0)
    outcome = await fetch_document("http://127.0.0.1/secret.pdf", fetcher=fetcher)
    assert outcome["status"] == "ssrf_blocked"


@pytest.mark.asyncio
async def test_login_required():
    outcome = await fetch_document(
        "https://files.example/private.pdf",
        fetcher=StubFetcher(BytesOutcome(None, "https://files.example/private.pdf", 403, "HTTP 403", "upstream")),
    )
    assert outcome["status"] == "login_required"


# ---------- fetch_bytes cap (streaming abort) ----------


@pytest.mark.asyncio
async def test_fetch_bytes_aborts_at_cap():
    big = b"x" * 4096
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=big))
    fetcher = SafeFetcher(transport=transport, rate_seconds_per_domain=0)
    outcome = await fetcher.fetch_bytes("https://files.example/big.bin", max_bytes=1024)
    assert outcome.too_large is True and outcome.data is None


@pytest.mark.asyncio
async def test_fetch_bytes_happy_path():
    payload = b"%PDF-1.4 tiny"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
    fetcher = SafeFetcher(transport=transport, rate_seconds_per_domain=0)
    outcome = await fetcher.fetch_bytes("https://files.example/tiny.pdf")
    assert outcome.data == payload and outcome.error is None


# ---------- parse_document_bytes (chat attachment injection) ----------


def test_parse_document_bytes_text_formats_decode_utf8():
    assert parse_document_bytes("你好世界".encode(), "notes.txt") == "你好世界"
    assert parse_document_bytes(b"\xef\xbb\xbf# Title", "doc.md") == "# Title"  # BOM tolerated
    assert parse_document_bytes(b'{"a": 1}', "data.json") == '{"a": 1}'
    assert parse_document_bytes(b"a,b\n1,2", "rows.csv") == "a,b\n1,2"


def test_parse_document_bytes_pdf_docx_xlsx():
    assert "hello pdf" in parse_document_bytes(make_pdf("hello pdf"), "paper.pdf")
    assert "word body text with enough characters" in parse_document_bytes(make_docx("word body text with enough characters"), "draft.docx")
    assert "苹果" in parse_document_bytes(make_xlsx(), "table.xlsx")


def test_parse_document_bytes_magic_overrides_extension():
    # A .pdf name on docx bytes still parses (magic bytes are the truth).
    assert "word body text with enough characters" in parse_document_bytes(make_docx("word body text with enough characters"), "lying.pdf")


def test_parse_document_bytes_rejects_unsupported_extension():
    with pytest.raises(ValueError, match="unsupported file type"):
        parse_document_bytes(b"\x89PNG\r\n\x1a\n", "image.png")


def test_parse_document_bytes_corrupted_raises_value_error():
    with pytest.raises(ValueError):
        parse_document_bytes(b"PK\x03\x04 broken zip", "broken.docx")
