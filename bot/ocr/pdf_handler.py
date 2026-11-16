"""PDF access: the embedded text of each page, and page images for the pages that need OCR."""

from __future__ import annotations

import io
import threading

import numpy as np

from ..extract import ExtractionError

MAX_RENDER_SIDE = 3200  # pixels; a poster-sized page is rendered smaller rather than refused

# PDFium is not thread-safe: every call into it goes through this lock.
_PDFIUM_LOCK = threading.Lock()


def read_text_layers(data: bytes) -> list[str]:
    """The embedded text of every page ('' where a page has none)."""
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractionError("That PDF is password protected.")
        texts = []
        for page in reader.pages:
            try:
                texts.append(page.extract_text() or "")
            except Exception:  # one damaged page must not lose the whole document
                texts.append("")
        return texts
    except ExtractionError:
        raise
    except (PyPdfError, ValueError, KeyError, OSError, RecursionError) as exc:
        raise ExtractionError(f"That PDF could not be read ({exc}).") from exc


class PdfRenderer:
    """Renders pages of one PDF to BGR images. Safe to share between worker threads."""

    def __init__(self, data: bytes, dpi: int):
        import pypdfium2

        self._dpi = dpi
        try:
            with _PDFIUM_LOCK:
                self._pdf = pypdfium2.PdfDocument(data)
                self.page_count = len(self._pdf)
        except pypdfium2.PdfiumError as exc:
            raise ExtractionError(f"That PDF could not be opened for OCR ({exc}).") from exc

    def render(self, index: int) -> np.ndarray:
        with _PDFIUM_LOCK:
            page = self._pdf[index]
            try:
                width, height = page.get_size()  # in points, 1/72 inch
                scale = min(self._dpi / 72, MAX_RENDER_SIDE / max(width, height, 1))
                bitmap = page.render(scale=scale, rev_byteorder=False)
                try:
                    image = np.array(bitmap.to_numpy(), copy=True)
                finally:
                    bitmap.close()
            finally:
                page.close()
        if image.ndim == 2:
            return np.stack([image] * 3, axis=-1)
        return np.ascontiguousarray(image[:, :, :3])  # BGR(A/X) -> BGR

    def close(self) -> None:
        with _PDFIUM_LOCK:
            self._pdf.close()
