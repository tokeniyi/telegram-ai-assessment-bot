"""Decide what an upload is and whether a PDF page needs OCR at all."""

from __future__ import annotations

import unicodedata
from pathlib import PurePath

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".heic", ".heif"}
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",          # JPEG
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a", b"GIF89a",
    b"BM",
    b"II*\x00", b"MM\x00*",   # TIFF
)

GOOD_TEXT_CHARS = 200   # a page with this much real text is used as it is
SPARSE_TEXT_CHARS = 25  # below this the text layer is treated as absent


def file_kind(filename: str | None, data: bytes, mime_type: str | None = None) -> str:
    """'pdf', 'image' or 'other', judged by the content first and the name second."""
    suffix = PurePath(filename or "").suffix.lower()
    mime = (mime_type or "").lower()
    if data[:5] == b"%PDF-" or suffix == ".pdf" or mime == "application/pdf":
        return "pdf"
    if data.startswith(_IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP") or data[4:8] == b"ftyp" \
            and data[8:12] in (b"heic", b"heix", b"mif1", b"heif"):
        return "image"
    if suffix in IMAGE_EXTENSIONS or mime.startswith("image/"):
        return "image"
    return "other"


def text_layer_quality(text: str) -> str:
    """Classify a PDF page's embedded text: 'good', 'sparse' or 'none'.

    'sparse' covers scans that carry only a stamp or page number as text, and text
    layers whose font encoding is broken (they extract as symbols, not words).
    In both cases the page is also OCRed and the better result wins.
    """
    compact = "".join(text.split())
    if not compact:
        return "none"
    readable = sum(1 for ch in compact if ch.isalnum())
    broken = sum(1 for ch in compact if ch == "�" or unicodedata.category(ch) in ("Co", "Cn", "Cc"))
    if readable < SPARSE_TEXT_CHARS:
        return "none"
    if broken > 0.1 * len(compact) or readable < 0.5 * len(compact):
        return "sparse"
    return "good" if readable >= GOOD_TEXT_CHARS else "sparse"
