"""Turn an uploaded file with a text layer (PDF, DOCX, plain text) into text.

Scanned PDFs and images go through bot.ocr, which calls this module for everything else.
"""

from __future__ import annotations

import io
from pathlib import PurePath

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".rtf", ".tex", ".json", ".html", ".htm"}
SUPPORTED_DESCRIPTION = "PDF, DOCX, TXT or MD file, or a photo"


class ExtractionError(Exception):
    """The file could not be read; the message is safe to show to the user."""


def extract_text(filename: str | None, data: bytes, mime_type: str | None = None) -> str:
    suffix = PurePath(filename or "").suffix.lower()
    mime = (mime_type or "").lower()

    if suffix == ".pdf" or mime == "application/pdf" or data[:5] == b"%PDF-":
        text = _pdf(data)
    elif suffix == ".docx" or "wordprocessingml" in mime:
        text = _docx(data)
    elif suffix == ".doc":
        raise ExtractionError("Old .doc files are not supported. Save it as .docx or PDF and send it again.")
    elif suffix in TEXT_EXTENSIONS or mime.startswith("text/"):
        text = _plain(data)
    else:
        raise ExtractionError(f"Unsupported file type. Send a {SUPPORTED_DESCRIPTION}.")

    text = "\n".join(line.rstrip() for line in text.replace("\x00", "").splitlines()).strip()
    if len(text) < 20:
        raise ExtractionError(
            "No readable text was found in that file. If it is a scanned PDF (images of pages), "
            "it needs OCR first."
        )
    return text


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractionError("That PDF is password protected.")
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except ExtractionError:
        raise
    except (PyPdfError, ValueError, KeyError, OSError) as exc:
        raise ExtractionError(f"That PDF could not be read ({exc}).") from exc


def _docx(data: bytes) -> str:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # python-docx raises several unrelated types on bad files
        raise ExtractionError(f"That DOCX file could not be read ({exc}).") from exc
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def _plain(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            continue
    raise ExtractionError("That text file could not be decoded.")
