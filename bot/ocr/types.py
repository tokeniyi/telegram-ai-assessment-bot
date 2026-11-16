"""Data shapes shared by the OCR stages."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

MODES = ("off", "auto", "on")
KNOWN_ENGINES = ("rapidocr", "easyocr", "tesseract")


@dataclass(frozen=True)
class OCRConfig:
    enabled: bool = True
    # Tried in this order; an engine that is not installed is skipped.
    engines: tuple[str, ...] = ("rapidocr", "tesseract")
    # Each worker holds its own copy of the models (about 300 MB); two is the sweet spot on 2-4 cores.
    workers: int = max(1, min(2, os.cpu_count() or 1))
    max_pages: int = 150            # pages OCRed per document (admins)
    max_pages_user: int = 15        # pages OCRed per document (everyone else)
    dpi: int = 170                  # resolution PDF pages are rendered at
    min_confidence: float = 0.80    # below this a page is retried and reported
    tesseract_langs: str = "eng"
    ai_correction: str = "auto"     # off | auto (low-confidence pages) | on (every OCR page)
    vision: str = "auto"            # off | auto (pages local OCR struggles with) | on (every OCR page)
    vision_model: str | None = None
    vision_max_pages: int = 12


@dataclass
class Line:
    """One recognised text line with its axis-aligned box in page pixels."""

    text: str
    conf: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class PageResult:
    number: int                      # 1-based
    text: str = ""
    source: str = "empty"            # text | ocr | vision | empty
    confidence: float | None = None  # None for a PDF text layer
    engine: str | None = None
    has_math: bool = False
    has_figure: bool = False
    corrected: bool = False          # text was cleaned by the AI correction layer
    warnings: list[str] = field(default_factory=list)
    # Downscaled JPEG kept only while a page may still go to the vision model.
    image: bytes | None = field(default=None, repr=False)
    # The lines local OCR was sure about; a vision transcription must agree with them.
    confident_text: str = field(default="", repr=False)

    def low_confidence(self, threshold: float) -> bool:
        return self.source == "ocr" and self.confidence is not None and self.confidence < threshold


@dataclass
class DocumentResult:
    pages: list[PageResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    kind: str = "text"               # pdf | image | text

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text.strip()).strip()

    @property
    def ocr_pages(self) -> list[PageResult]:
        return [p for p in self.pages if p.source in ("ocr", "vision")]

    @property
    def used_ocr(self) -> bool:
        return bool(self.ocr_pages)

    @property
    def confidence(self) -> float | None:
        """Mean confidence of the locally recognised pages, weighted by text length."""
        scored = [(p.confidence, max(len(p.text), 1)) for p in self.pages
                  if p.source == "ocr" and p.confidence is not None]
        if not scored:
            return None
        return sum(c * w for c, w in scored) / sum(w for _, w in scored)

    def report(self) -> list[dict]:
        """Per-page record: where the text came from and how sure the OCR was."""
        return [{"page": p.number, "source": p.source, "engine": p.engine,
                 "confidence": None if p.confidence is None else round(p.confidence, 3),
                 "chars": len(p.text), "corrected": p.corrected} for p in self.pages]
