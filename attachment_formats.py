"""Recognize stored attachments and read bounded excerpts from common documents."""

import posixpath
from itertools import islice
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pypdf import PdfReader, apply_configuration
from pypdf.errors import PyPdfError

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXTENSIONS = {
    DOCX: ".docx", XLSX: ".xlsx", "application/pdf": ".pdf", "text/plain": ".txt",
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
    "audio/opus": ".opus", "audio/ogg": ".ogg",
}
DOCUMENT_TYPES = {DOCX, XLSX, "application/pdf", "text/plain"}
MAX_IMAGE_BYTES = 8 * 1024**2
MAX_DOCUMENT_BYTES = 12 * 1024**2
MAX_OFFICE_XML = 4 * 1024**2
MAX_DOCUMENT_CHARS = 12_000
MAX_PDF_PAGES = 30
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def image_mime_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def detect_mime(path, declared=None):
    if declared:
        declared = declared.partition(";")[0].strip().lower()
    if declared and declared not in ("application/octet-stream", "application/zip"):
        return declared
    with path.open("rb") as reader:
        start = reader.read(4096)
    if kind := image_mime_type(start):
        return kind
    if start.startswith(b"%PDF-"):
        return "application/pdf"
    if start.startswith(b"OggS"):
        return "audio/opus" if b"OpusHead" in start else "audio/ogg"
    if start.startswith(b"PK\x03\x04") and path.stat().st_size <= MAX_DOCUMENT_BYTES:
        try:
            with ZipFile(path) as bundle:
                names = bundle.NameToInfo
                if "word/document.xml" in names:
                    return DOCX
                if "xl/workbook.xml" in names:
                    return XLSX
        except BadZipFile:
            return declared
    if start and len(start) == path.stat().st_size:
        try:
            text = start.decode("utf-8-sig")
        except UnicodeDecodeError:
            return declared
        if text.strip() and all(char.isprintable() or char in "\r\n\t" for char in text):
            return "text/plain"
    return declared


def preview_mime(path, declared):
    if declared in ("audio/opus", "audio/ogg"):
        with path.open("rb") as reader:
            return "audio/ogg" if reader.read(4) == b"OggS" else None
    if path.stat().st_size <= MAX_IMAGE_BYTES:
        with path.open("rb") as reader:
            return image_mime_type(reader.read(12))
    return None


def office_xml(bundle, filename):
    info = bundle.getinfo(filename)
    if info.file_size > MAX_OFFICE_XML:
        raise ValueError("A document XML part exceeds the extraction limit")
    return ElementTree.fromstring(bundle.read(info))


def docx_lines(bundle):
    document = office_xml(bundle, "word/document.xml")
    for paragraph in document.iter(WORD + "p"):
        yield "".join(fragment.text or "" for fragment in paragraph.iter(WORD + "t"))


def xlsx_lines(bundle):
    workbook = office_xml(bundle, "xl/workbook.xml")
    relations = office_xml(bundle, "xl/_rels/workbook.xml.rels")
    targets = {item.get("Id"): item.get("Target", "") for item in relations}
    shared = []
    if "xl/sharedStrings.xml" in bundle.NameToInfo:
        strings = office_xml(bundle, "xl/sharedStrings.xml")
        shared = ["".join(fragment.text or "" for fragment in item.iter(SHEET + "t"))
                  for item in strings.iter(SHEET + "si")]
    for sheet in workbook.iter(SHEET + "sheet"):
        target = targets.get(sheet.get(REL + "id"), "")
        path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
        if not path.startswith("xl/worksheets/") or path not in bundle.NameToInfo:
            continue
        yield "Sheet: " + sheet.get("name", "")
        for row in office_xml(bundle, path).iter(SHEET + "row"):
            for cell in row.iter(SHEET + "c"):
                value = cell.findtext(SHEET + "v") or ""
                if cell.get("t") == "s" and value.isascii() and value.isdigit():
                    value = shared[int(value)]
                elif cell.get("t") == "inlineStr":
                    value = "".join(fragment.text or "" for fragment in cell.iter(SHEET + "t"))
                if value:
                    yield f"{cell.get('r', '')} {value}".strip()


def bounded_text(lines):
    remaining = MAX_DOCUMENT_CHARS
    parts = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if parts:
            remaining -= 1
        if len(line) > remaining:
            if remaining > 0:
                parts.append(line[:remaining])
            return "\n".join(parts), True
        parts.append(line)
        remaining -= len(line)
    return "\n".join(parts), False


def pdf_lines(pages):
    for page in islice(pages, MAX_PDF_PAGES):
        contents = page.get_contents()
        if contents is not None and len(contents.get_data()) > MAX_OFFICE_XML:
            raise ValueError("A PDF page exceeds the extraction limit")
        yield page.extract_text()


def document_text(path, mime):
    if mime not in DOCUMENT_TYPES:
        raise ValueError("Text extraction supports PDF, DOCX, XLSX and UTF-8 TXT")
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError("Document exceeds the 12 MiB reading limit. Download it from the archive panel")
    try:
        if mime == "text/plain":
            return bounded_text(path.read_text(encoding="utf-8-sig").splitlines())
        if mime == "application/pdf":
            with path.open("rb") as file, apply_configuration(
                zlib_maximum_output_length=MAX_OFFICE_XML,
                lzw_maximum_output_length=MAX_OFFICE_XML,
                run_length_maximum_output_length=MAX_OFFICE_XML,
                array_based_stream_maximum_output_length=MAX_OFFICE_XML,
                xform_maximum_invocations_per_extraction=100,
                page_tree_maximum_entries=1000,
            ):
                reader = PdfReader(file)
                if reader.is_encrypted:
                    raise ValueError("This PDF is password protected")
                text, truncated = bounded_text(pdf_lines(reader.pages))
                return text, truncated or len(reader.pages) > MAX_PDF_PAGES
        with ZipFile(path) as bundle:
            if sum(i.file_size for i in bundle.infolist() if i.filename.endswith(".xml")) > 32 * 1024**2:
                raise ValueError("The Office document exceeds the XML extraction limit")
            return bounded_text(docx_lines(bundle) if mime == DOCX else xlsx_lines(bundle))
    except (BadZipFile, DefusedXmlException, ElementTree.ParseError, IndexError,
            KeyError, UnicodeError, PyPdfError, RecursionError):
        raise ValueError("Could not extract readable text from the document") from None
