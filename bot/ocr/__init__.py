"""OCR for scanned PDFs and images, built from free, locally running engines."""

from .pipeline import OCRService, describe_result
from .types import DocumentResult, OCRConfig, PageResult

__all__ = ["DocumentResult", "OCRConfig", "OCRService", "PageResult", "describe_result"]
