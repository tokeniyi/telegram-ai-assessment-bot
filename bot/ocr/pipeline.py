"""The OCR pipeline: upload in, text with a per-page confidence report out.

    upload ─ file_kind ─┬─ text / DOCX ───────────────────────────── text
                        ├─ PDF ── text layer good? ── yes ────────── text
                        │              └─ no ── render page ─┐
                        └─ image ───────────────────────────┤
                                                             ▼
             upright + deskew + resize ─ recognise ─ unsure? ─ enhance, retry, fallback engines
                                                             ▼
                       reading order (columns, tables) ─ clean-up ─ confidence
                                                             ▼
               AI layer (optional): vision model for handwriting / formulas / figures,
                                    text model to repair low-confidence pages

Pages are processed in parallel on a thread pool shared by all users, so one
large scan cannot starve the bot and memory stays bounded to a few pages.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..extract import ExtractionError, extract_text
from . import language_model as lm
from .detector import file_kind, text_layer_quality
from .postprocess import clean_page, looks_like_math, plausibility, strip_running_lines
from .types import DocumentResult, Line, OCRConfig, PageResult

log = logging.getLogger(__name__)

MAX_SIDE = 3200           # pixels; larger pages are scaled down before recognition
AI_CONCURRENCY = 3
Progress = Callable[[str, int, int], Awaitable[None]]

NOTHING_READ = (
    "No readable text was found. If this is a photo, make sure it is sharp and well lit, "
    "and send it as a file rather than a compressed photo if the writing is small."
)


@dataclass
class _Attempt:
    engine: str
    lines: list[Line]
    text: str
    confidence: float

    @property
    def chars(self) -> int:
        return sum(1 for ch in self.text if ch.isalnum())


class OCRService:
    def __init__(self, config: OCRConfig, get_provider: Callable[[], object | None] = lambda: None):
        self.config = config
        self._get_provider = get_provider
        self._engines = None
        self._pool = ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix="ocr")
        self._ai_gate = asyncio.Semaphore(AI_CONCURRENCY)

    # ── what is installed ────────────────────────────────────────────────
    @property
    def engines(self) -> list:
        if self._engines is None:
            if not self.config.enabled or not all(importlib.util.find_spec(m) for m in ("cv2", "numpy")):
                self._engines = []
            else:
                from .recognizer import build_engines

                threads = max(1, (os.cpu_count() or 1) // self.config.workers)
                self._engines = build_engines(self.config.engines, threads, self.config.tesseract_langs)
        return self._engines

    @property
    def available(self) -> bool:
        return bool(self.engines)

    def describe(self) -> str:
        if not self.config.enabled:
            return "OCR is switched off (OCR_ENABLED=false)"
        if not self.available:
            return "OCR is NOT available: install the packages in requirements.txt"
        return (f"OCR engines: {', '.join(e.name for e in self.engines)} · {self.config.workers} workers · "
                f"vision {self.config.vision} · AI correction {self.config.ai_correction}")

    def warm_up(self) -> None:
        """Load the models once so that the first upload is not slow."""
        if self.available:
            import numpy as np

            self._pool.submit(self._read_image, np.full((64, 256, 3), 255, dtype=np.uint8), "off")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _unavailable(self) -> ExtractionError:
        if not self.config.enabled:
            return ExtractionError("This file has no text layer and OCR is switched off on this bot.")
        return ExtractionError(
            "This file needs OCR, but no OCR engine is installed on the server "
            "(pip install -r requirements.txt)."
        )

    # ── entry points ─────────────────────────────────────────────────────
    async def extract(
        self, filename: str | None, data: bytes, mime_type: str | None = None, *,
        progress: Progress | None = None, max_pages: int | None = None, force_vision: bool = False,
    ) -> DocumentResult:
        """Read any supported upload. Raises ExtractionError with a user-safe message."""
        kind = file_kind(filename, data, mime_type)
        if kind == "pdf":
            document = await self._pdf(data, progress, max_pages or self.config.max_pages)
        elif kind == "image":
            return await self.extract_images([data], progress=progress, max_pages=max_pages,
                                             force_vision=force_vision)
        else:
            text = await asyncio.to_thread(extract_text, filename, data, mime_type)
            return DocumentResult(pages=[PageResult(1, text, "text")], kind="text")
        return await self._finish(document, progress, force_vision)

    async def extract_images(
        self, images: list[bytes], *, progress: Progress | None = None,
        max_pages: int | None = None, force_vision: bool = False,
    ) -> DocumentResult:
        """Read one or more images (an album of photographed pages) as one document."""
        if not self.available:
            raise self._unavailable()
        limit = max_pages or self.config.max_pages
        document = DocumentResult(kind="image")
        if len(images) > limit:
            document.warnings.append(f"Only the first {limit} of {len(images)} images were read.")
        vision = self._vision_mode(force_vision)
        loop = asyncio.get_running_loop()
        jobs = [loop.run_in_executor(self._pool, self._read_image_file, data, vision, len(images) == 1)
                for data in images[:limit]]
        for pages in await self._gather(jobs, progress):
            for page in pages:
                page.number = len(document.pages) + 1
                document.pages.append(page)
        return await self._finish(document, progress, force_vision)

    # ── PDF ──────────────────────────────────────────────────────────────
    async def _pdf(self, data: bytes, progress: Progress | None, max_pages: int) -> DocumentResult:
        from .pdf_handler import read_text_layers

        layers = await asyncio.to_thread(read_text_layers, data)
        document = DocumentResult(kind="pdf")
        qualities = [text_layer_quality(text) for text in layers]
        for number, (text, quality) in enumerate(zip(layers, qualities), start=1):
            usable = quality != "none"
            document.pages.append(PageResult(number, clean_page(text) if usable else "", "text" if usable else "empty"))
        scanned = [i for i, quality in enumerate(qualities) if quality != "good"]
        if not scanned:
            return document
        if not self.available or not importlib.util.find_spec("pypdfium2"):
            if not document.text:
                raise self._unavailable()
            document.warnings.append(
                f"{len(scanned)} page(s) look scanned and were skipped: OCR is not available on this bot."
            )
            return document
        if len(scanned) > max_pages:
            document.warnings.append(
                f"{len(scanned)} pages need OCR; only the first {max_pages} were read (page limit)."
            )
            scanned = scanned[:max_pages]

        from .pdf_handler import PdfRenderer

        renderer = await asyncio.to_thread(PdfRenderer, data, self.config.dpi)
        try:
            vision = self._vision_mode(False)
            loop = asyncio.get_running_loop()
            scanned = [index for index in scanned if index < renderer.page_count]
            jobs = [loop.run_in_executor(self._pool, self._read_pdf_page, renderer, index, vision, len(scanned) == 1)
                    for index in scanned]
            results = await self._gather(jobs, progress)
        finally:
            await asyncio.to_thread(renderer.close)

        for page in results:
            layer = document.pages[page.number - 1]
            layer_chars = sum(1 for ch in layer.text if ch.isalnum())
            read_chars = sum(1 for ch in page.text if ch.isalnum())
            # A scan with only a stamp as text: OCR wins. A thin but real text page: the layer wins.
            if read_chars > 1.5 * layer_chars or not layer_chars:
                document.pages[page.number - 1] = page
        return document

    async def _gather(self, jobs: list, progress: Progress | None) -> list:
        """Await the page jobs in order, reporting progress as each one finishes."""
        done = 0

        async def tracked(job):
            nonlocal done
            result = await job
            done += 1
            if progress:
                await progress("ocr", done, len(jobs))
            return result

        if progress and jobs:
            await progress("ocr", 0, len(jobs))
        return list(await asyncio.gather(*(tracked(job) for job in jobs)))

    # ── one page (runs on a worker thread) ───────────────────────────────
    def _read_pdf_page(self, renderer, index: int, vision: str, wide: bool) -> PageResult:
        try:
            page = self._read_image(renderer.render(index), vision, wide)
        except Exception:
            log.exception("OCR failed on PDF page %d", index + 1)
            page = PageResult(0, warnings=["could not be processed"])
        page.number = index + 1
        return page

    def _read_image_file(self, data: bytes, vision: str, wide: bool) -> list[PageResult]:
        from .preprocess import ImageError, decode_images

        try:
            frames = decode_images(data)
        except ImageError as exc:
            return [PageResult(0, warnings=[str(exc)])]
        pages = []
        for frame in frames:
            try:
                pages.append(self._read_image(frame, vision, wide and len(frames) == 1))
            except Exception:
                log.exception("OCR failed on an image")
                pages.append(PageResult(0, warnings=["could not be processed"]))
        return pages

    def _read_image(self, image, vision: str, wide: bool = False) -> PageResult:
        """Recognise one page image. `wide` lets a lone page use every CPU core."""
        from . import preprocess as pp

        page = PageResult(0)
        if pp.is_blank(image):
            return page
        image = pp.limit_side(image, MAX_SIDE)
        engines = self.engines
        primary = engines[0]
        attempts: list[_Attempt] = []
        enhanced = None
        boxes: list[tuple[float, float, float, float]] | None = None

        if primary.staged:
            with primary.session(wide) as model:
                image, quads, levelled = self._locate(primary, model, image)
                boxes = [(float(q[:, 0].min()), float(q[:, 1].min()), float(q[:, 0].max()), float(q[:, 1].max()))
                         for q in quads]
                attempts.append(self._read(primary, model, image, quads, levelled, primary.name))
                if not self._sure(attempts[0]):
                    enhanced = pp.enhance(image)
                    attempts.append(self._read(primary, model, enhanced, quads, levelled, f"{primary.name}+enhance"))
        else:
            image = self._upright(image, primary)
            attempts.append(self._whole(primary, image, primary.name))
            if not self._sure(attempts[0]):
                enhanced = pp.enhance(image)
                attempts.append(self._whole(primary, enhanced, f"{primary.name}+enhance"))

        for engine in engines[1:]:  # fallback engines, only while nothing is trustworthy yet
            if any(self._sure(a) for a in attempts):
                break
            if not any(a.chars for a in attempts):  # the first engine saw no text: re-check which way is up
                image, enhanced = self._upright(image, engine), None
            if enhanced is None:
                enhanced = pp.enhance(image)
            attempts.append(self._whole(engine, enhanced, f"{engine.name}+enhance"))

        most = max(a.chars for a in attempts)
        best = max(attempts, key=lambda a: a.confidence * (0.5 + 0.5 * plausibility(a.text))
                   * math.sqrt(a.chars / most) if most else 0.0)
        if boxes is None or best.engine.split("+")[0] != primary.name:
            boxes = [(l.x0, l.y0, l.x1, l.y1) for l in best.lines]

        page.text = best.text
        page.confidence = best.confidence
        page.engine = best.engine
        page.source = "ocr" if best.text else "empty"
        page.has_math = looks_like_math(best.text)
        page.has_figure = pp.has_figure(image, boxes)
        page.confident_text = " ".join(l.text for l in best.lines if l.conf >= 0.9)
        struggling = not best.text or best.confidence < self.config.min_confidence
        if vision == "on" or (vision == "auto" and (struggling or page.has_math or page.has_figure)):
            page.image = pp.encode_jpeg(image)
        return page

    def _sure(self, attempt: _Attempt) -> bool:
        return attempt.chars > 0 and attempt.confidence >= self.config.min_confidence

    @staticmethod
    def _make_attempt(label: str, lines: list[Line]) -> _Attempt:
        from .layout import page_text
        from .recognizer import DROP_BELOW

        weight = sum(len(l.text) for l in lines)
        confidence = sum(l.conf * len(l.text) for l in lines) / weight if weight else 0.0
        kept = [l for l in lines if l.conf >= DROP_BELOW]
        return _Attempt(label, kept, clean_page(page_text(kept)), confidence)

    def _whole(self, engine, image, label: str) -> _Attempt:
        """One attempt with an engine that locates and reads the page in a single call."""
        return self._make_attempt(label, engine.recognize(image))

    def _read(self, engine, model, image, quads, levelled, label: str) -> _Attempt:
        """One attempt of a staged engine: read the already located lines from this version of the page."""
        from .recognizer import crop_line, line_from_quad

        texts = engine.read(model, [crop_line(image, quad) for quad in quads])
        return self._make_attempt(label, [line_from_quad(text, conf, quad)
                                          for (text, conf), quad in zip(texts, levelled) if text.strip()])

    def _locate(self, engine, model, image):
        """Find the text lines and turn the page upright.

        Returns (page image, line boxes on it, the same boxes as on a deskewed page).
        A tilted page is not re-rendered: each line is cut out along its own tilt,
        and only the coordinates used for the reading order are levelled.
        """
        from . import preprocess as pp
        from . import recognizer as rec

        quads = engine.detect(model, image)
        if rec.sideways_share(quads) > 0.6:  # the page lies on its side
            image = pp.rotate_quarter(image, 90)
            quads = engine.detect(model, image)
        sample = [rec.crop_line(image, quad) for quad in rec.longest(quads)]
        if sample:  # read a few lines both ways up; upside-down text scores far lower
            upright = sum(conf for _, conf in engine.read(model, sample))
            flipped = sum(conf for _, conf in engine.read(model, [pp.rotate_quarter(c, 180) for c in sample]))
            if flipped > upright + 0.05 * len(sample):
                image = pp.rotate_quarter(image, 180)
                quads = rec.flip_quads(quads, image.shape[1], image.shape[0])
        angle = rec.skew_angle(quads)
        levelled = rec.level_quads(quads, angle if 0.7 < abs(angle) < 25 else 0.0, image.shape[1], image.shape[0])
        return image, quads, levelled

    def _upright(self, image, engine):
        """Upright and deskew a page for an engine that reads the whole page in one call."""
        from . import preprocess as pp

        turn = engine.orientation(pp.limit_side(image, 1600))
        if turn:
            image = pp.rotate_quarter(image, turn)
        angle = pp.estimate_skew(image)
        return pp.rotate(image, angle) if angle else image

    # ── AI layer and final assembly ──────────────────────────────────────
    def _vision_mode(self, force: bool) -> str:
        provider = self._get_provider()
        if self.config.vision == "off" or provider is None or not lm.supports_vision(provider):
            return "off"
        return "on" if force or self.config.vision == "on" else "auto"

    async def _finish(self, document: DocumentResult, progress: Progress | None, force_vision: bool) -> DocumentResult:
        try:
            await self._ai_layer(document, progress)
        finally:
            for page in document.pages:
                page.image = None

        if document.kind == "pdf":
            for page, text in zip(document.pages, strip_running_lines([p.text for p in document.pages])):
                page.text = text
        threshold = self.config.min_confidence
        unsure = [str(p.number) for p in document.pages if p.low_confidence(threshold)]
        unread = [str(p.number) for p in document.pages if p.source == "empty" and p.confidence is not None]
        failed = [f"{p.number} ({'; '.join(p.warnings)})" for p in document.pages
                  if p.source == "empty" and p.warnings]
        many = len(document.pages) > 1
        if unsure:
            document.warnings.append(
                (f"Low text confidence on page {', '.join(unsure)}" if many else "Low text confidence")
                + " — the text read there may contain mistakes."
            )
        if unread:
            document.warnings.append(
                (f"Nothing could be read on page {', '.join(unread)}" if many else "Nothing could be read")
                + " (handwriting or a picture without text?)."
            )
        if failed:
            document.warnings.append("Skipped: " + ", ".join(f"page {f}" if many else f for f in failed))
        if not document.text:
            raise ExtractionError(failed[0].split("(", 1)[1].rstrip(")") if failed and not many else NOTHING_READ)
        return document

    async def _ai_layer(self, document: DocumentResult, progress: Progress | None) -> None:
        provider = self._get_provider()
        if provider is None:
            return
        threshold = self.config.min_confidence

        # 1. vision: pages that local OCR could not read, was unsure about, or that hold formulas / figures
        waiting = sorted(
            (p for p in document.pages if p.image is not None),
            key=lambda p: (bool(p.text), not p.low_confidence(threshold), not p.has_math, p.number),
        )
        chosen = waiting[: self.config.vision_max_pages]
        if len(waiting) > len(chosen):
            document.warnings.append(
                f"{len(waiting) - len(chosen)} more page(s) with formulas or figures were read as plain text only "
                f"(OCR_VISION_MAX_PAGES={self.config.vision_max_pages})."
            )
        errors: list[str] = []
        done = 0

        async def look(page: PageResult) -> None:
            nonlocal done
            try:
                async with self._ai_gate:
                    text = await lm.read_page(provider, page.image, page.text, page.confident_text,
                                              self.config.vision_model)
            except lm.AIError as exc:
                errors.append(str(exc))
                text = None
            if text:
                if len(lm._words(page.confident_text)) < 8:
                    page.warnings.append("read by the AI model only")
                page.text, page.source, page.engine = clean_page(text), "vision", "vision"
            done += 1
            if progress:
                await progress("ai", done, len(chosen))

        if chosen:
            if progress:
                await progress("ai", 0, len(chosen))
            await asyncio.gather(*(look(page) for page in chosen))
        if errors:
            log.warning("Vision reading failed for %d page(s): %s", len(errors), errors[0])
            document.warnings.append(
                f"The AI model could not look at {len(errors)} page image(s) ({errors[0]}) — "
                "they use local OCR only. Set OCR_VISION_MODEL to a model that accepts images, or OCR_VISION=off."
            )

        # 2. correction: repair what local OCR was unsure about
        mode = self.config.ai_correction
        if mode == "off":
            return
        targets = [p for p in document.pages if p.source == "ocr" and len(p.text) >= 40
                   and (mode == "on" or p.low_confidence(threshold))]
        failures: list[str] = []

        async def repair(page: PageResult) -> None:
            try:
                async with self._ai_gate:
                    page.text, page.corrected = await lm.correct_text(provider, page.text)
            except lm.AIError as exc:
                failures.append(str(exc))

        await asyncio.gather(*(repair(page) for page in targets))
        if failures:
            log.warning("AI correction failed for %d page(s): %s", len(failures), failures[0])


def describe_result(document: DocumentResult) -> str | None:
    """One line for the uploader about how the document was read; None when no OCR was involved."""
    if not document.used_ocr:
        return None
    read = len(document.ocr_pages)
    by_vision = sum(1 for p in document.pages if p.source == "vision")
    confidence = document.confidence
    sure = f" · OCR confidence {round(100 * confidence)}%" if confidence is not None else ""
    if document.kind == "image" and len(document.pages) == 1:
        line = "🔎 Text read from the image" + sure
    elif document.kind == "image":
        line = f"🔎 Text read from {read} images" + sure
    else:
        with_text = sum(1 for p in document.pages if p.source == "text")
        line = f"🔎 {len(document.pages)} pages: {with_text} with text, {read} scanned and read by OCR" + sure
    if by_vision:
        line += f" · {by_vision} read by the AI vision model"
    return line
