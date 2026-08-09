from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from html import escape
from typing import Protocol


class ExportChapter(Protocol):
    chapter_no: int
    title: str


@dataclass(frozen=True)
class ExportArtifact:
    body: bytes
    media_type: str
    extension: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


_ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)


def _zip_write(archive: zipfile.ZipFile, name: str, value: str | bytes, *, stored: bool = False) -> None:
    info = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    archive.writestr(info, value)


def _items(chapters: Iterable[tuple[ExportChapter, str]]) -> list[tuple[ExportChapter, str]]:
    return list(chapters)


def _plain_text(title: str, author: str, template: str, chapters: list[tuple[ExportChapter, str]]) -> str:
    prefix = {
        "web-serial": f"{title}\n{author}\n\n",
        "submission": f"{title}\nby {author}\n\n---\n\n",
        "archive": f"PROSEFORGE ARCHIVE\nTitle: {title}\nAuthor: {author}\n\n",
    }[template]
    separator = "\n\n***\n\n" if template == "web-serial" else "\n\n"
    return prefix + separator.join(f"Chapter {chapter.chapter_no}: {chapter.title}\n\n{content}" for chapter, content in chapters)


def _markdown(title: str, author: str, locale: str, template: str, chapters: list[tuple[ExportChapter, str]]) -> str:
    prefix = {
        "web-serial": f"# {title}\n\n_{author}_\n\n",
        "submission": f"---\ntitle: {json.dumps(title, ensure_ascii=False)}\nauthor: {json.dumps(author, ensure_ascii=False)}\nlang: {locale}\n---\n\n",
        "archive": f"# ProseForge archive: {title}\n\n- Author: {author}\n- Locale: {locale}\n- Template: archive\n\n",
    }[template]
    separator = "\n\n---\n\n" if template == "web-serial" else "\n\n"
    return prefix + separator.join(f"## {chapter.chapter_no}. {chapter.title}\n\n{content}" for chapter, content in chapters)


def render_docx(
    chapters: Iterable[tuple[ExportChapter, str]],
    *,
    title: str = "ProseForge Export",
    author: str = "",
    locale: str = "en",
    template: str = "archive",
) -> bytes:
    paragraphs = [f'<w:p><w:r><w:t>{escape(template)}</w:t></w:r></w:p>']
    for chapter, content in _items(chapters):
        paragraphs.append(f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{escape(chapter.title)}</w:t></w:r></w:p>')
        for line in content.splitlines() or [""]:
            paragraphs.append(f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>')
    document = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xml:lang="{escape(locale)}"><w:body>{"".join(paragraphs)}<w:sectPr/></w:body></w:document>'''
    core = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{escape(title)}</dc:title><dc:creator>{escape(author)}</dc:creator><dc:language>{escape(locale)}</dc:language></cp:coreProperties>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>'''
    rels = '''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>'''
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        _zip_write(archive, "[Content_Types].xml", content_types)
        _zip_write(archive, "_rels/.rels", rels)
        _zip_write(archive, "docProps/core.xml", core)
        _zip_write(archive, "word/document.xml", document)
    return output.getvalue()


def render_epub(
    title: str,
    chapters: Iterable[tuple[ExportChapter, str]],
    *,
    author: str = "",
    locale: str = "en",
    template: str = "archive",
) -> bytes:
    body = f'<p data-template="{escape(template)}">{escape(author)}</p>' + "".join(
        f"<h1>{escape(chapter.title)}</h1><p>{escape(content).replace(chr(10), '<br/>')}</p>"
        for chapter, content in _items(chapters)
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        _zip_write(archive, "mimetype", "application/epub+zip", stored=True)
        _zip_write(archive, "META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        _zip_write(archive, "OEBPS/content.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml" lang="{escape(locale)}"><head><title>{escape(title)}</title></head><body>{body}</body></html>')
        _zip_write(archive, "OEBPS/content.opf", f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">proseforge-{hashlib.sha256(title.encode()).hexdigest()[:16]}</dc:identifier><dc:title>{escape(title)}</dc:title><dc:creator>{escape(author)}</dc:creator><dc:language>{escape(locale)}</dc:language></metadata><manifest><item id="content" href="content.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="content"/></spine></package>')
    return output.getvalue()


def render_export(
    *,
    format_name: str,
    chapters: Iterable[tuple[ExportChapter, str]],
    title: str,
    author: str,
    locale: str,
    template: str,
) -> ExportArtifact:
    items = _items(chapters)
    if format_name == "docx":
        return ExportArtifact(render_docx(items, title=title, author=author, locale=locale, template=template), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx")
    if format_name == "epub":
        return ExportArtifact(render_epub(title, items, author=author, locale=locale, template=template), "application/epub+zip", "epub")
    if format_name == "md":
        body = _markdown(title, author, locale, template, items).encode("utf-8")
        return ExportArtifact(body, "text/markdown; charset=utf-8", "md")
    body = _plain_text(title, author, template, items).encode("utf-8")
    return ExportArtifact(body, "text/plain; charset=utf-8", "txt")
