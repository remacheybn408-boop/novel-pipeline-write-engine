import zipfile
from io import BytesIO

from proseforge.application.writing.export_service import render_docx, render_epub


class Chapter:
    def __init__(self, title):
        self.title = title


def test_docx_is_a_readable_package():
    archive = zipfile.ZipFile(BytesIO(render_docx([(Chapter("Opening"), "Rain fell.")])))
    assert "word/document.xml" in archive.namelist()
    assert "Opening" in archive.read("word/document.xml").decode()


def test_epub_has_uncompressed_mimetype_first():
    data = render_epub("Novel", [(Chapter("Opening"), "Rain fell.")])
    archive = zipfile.ZipFile(BytesIO(data))
    assert archive.namelist()[0] == "mimetype"
    assert archive.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
    assert "OEBPS/content.opf" in archive.namelist()


def test_docx_and_epub_include_export_metadata():
    chapters = [(Chapter("Opening"), "Rain fell.")]
    docx = zipfile.ZipFile(BytesIO(render_docx(chapters, title="Novel", author="A. Writer", locale="en-GB")))
    core = docx.read("docProps/core.xml").decode()
    assert "Novel" in core
    assert "A. Writer" in core
    assert "en-GB" in core

    epub = zipfile.ZipFile(BytesIO(render_epub("Novel", chapters, author="A. Writer", locale="en-GB")))
    package = epub.read("OEBPS/content.opf").decode()
    assert "A. Writer" in package
    assert "en-GB" in package
