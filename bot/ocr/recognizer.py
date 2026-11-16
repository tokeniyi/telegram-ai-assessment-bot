"""OCR engines behind one interface. All of them are free and run on this machine.

- rapidocr:  PaddleOCR's PP-OCR detection + recognition models on ONNX Runtime
             (no PaddlePaddle or PyTorch needed; the models ship inside the package).
- easyocr:   optional fallback (needs PyTorch; downloads its models on first use).
- tesseract: optional fallback (needs the `tesseract` program and `pytesseract`).

An engine that is not installed reports itself unavailable and is skipped.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import os
import shutil
import threading

import cv2
import numpy as np

from .types import Line

log = logging.getLogger(__name__)

Quad = np.ndarray  # 4x2 float array: top-left, top-right, bottom-right, bottom-left

DROP_BELOW = 0.45  # lines the engine itself barely believes are left out of the text


def _line_from_quad(text: str, conf: float, quad) -> Line:
    points = np.asarray(quad, dtype=np.float32).reshape(-1, 2)
    return Line(text.strip(), float(conf), float(points[:, 0].min()), float(points[:, 1].min()),
                float(points[:, 0].max()), float(points[:, 1].max()))


class Engine:
    """Reads a whole page in one call. `staged` engines can also locate text without reading it."""

    name = "engine"
    staged = False

    def __init__(self, threads: int = 1):
        self.threads = threads

    @classmethod
    def available(cls) -> bool:
        raise NotImplementedError

    def recognize(self, image: np.ndarray) -> list[Line]:
        """Every text line found on a BGR page image, unreliable ones included."""
        raise NotImplementedError

    def orientation(self, image: np.ndarray) -> int | None:
        """Clockwise turn (0/90/180/270) that makes the page upright, if the engine can tell."""
        return None


class RapidOCREngine(Engine):
    """PP-OCR detection and recognition, run as two separate stages.

    Text is located once on a reduced copy of the page; each line is then cut out
    of the full-resolution page and read. Locating on a small copy is several
    times faster and loses nothing, because the reader rescales every line anyway.
    """

    name = "rapidocr"
    staged = True
    DETECT_SIDE = 1280

    def __init__(self, threads: int = 1):
        super().__init__(threads)
        self._local = threading.local()
        self._wide = None
        self._wide_lock = threading.Lock()

    @classmethod
    def available(cls) -> bool:
        return all(importlib.util.find_spec(m) for m in ("rapidocr", "onnxruntime"))

    def _build(self, threads: int):
        from rapidocr import RapidOCR

        return RapidOCR(params={
            "Global.log_level": "error",
            "Global.text_score": 0.0,        # keep everything; confidence is judged by the pipeline
            "Global.max_side_len": 4000,     # pages are already sized by the pipeline
            # The per-line direction classifier flips a share of perfectly upright lines, which
            # then read as garbage. The pipeline turns the whole page upright instead.
            "Global.use_cls": False,
            "EngineConfig.onnxruntime.intra_op_num_threads": threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })

    def session(self, wide: bool = False):
        """Context manager yielding a model instance.

        Worker threads each own a single-threaded instance, so pages of a long document
        scale across cores. A lone page (a photo) may instead borrow the one `wide`
        instance that uses every core, which makes it two to three times quicker.
        """
        return _Session(self, wide)

    def detect(self, model, image: np.ndarray) -> list[Quad]:
        """Text line boxes in page coordinates, as 4x2 arrays (TL, TR, BR, BL)."""
        height, width = image.shape[:2]
        scale = min(1.0, self.DETECT_SIDE / max(height, width))
        small = image if scale == 1.0 else cv2.resize(
            image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
        result = model(small, use_det=True, use_cls=False, use_rec=False)
        boxes = getattr(result, "boxes", None)
        return [] if boxes is None else [np.asarray(box, dtype=np.float32) / scale for box in boxes]

    def read(self, model, crops: list[np.ndarray]) -> list[tuple[str, float]]:
        """(text, confidence) for each line crop, in the same order."""
        if not crops:
            return []
        result = model.recognize_txt(crops)
        return [(str(text), float(score)) for text, score in zip(result.txts, result.scores)]

    def recognize(self, image: np.ndarray) -> list[Line]:
        with self.session() as model:
            quads = self.detect(model, image)
            texts = self.read(model, [crop_line(image, quad) for quad in quads])
        return [_line_from_quad(text, conf, quad) for (text, conf), quad in zip(texts, quads) if text.strip()]


class _Session:
    def __init__(self, engine: RapidOCREngine, wide: bool):
        self._engine, self._wide, self._locked = engine, wide, False

    def __enter__(self):
        engine = self._engine
        if self._wide and (os.cpu_count() or 1) > engine.threads and engine._wide_lock.acquire(blocking=False):
            self._locked = True
            if engine._wide is None:
                engine._wide = engine._build(os.cpu_count() or 1)
            return engine._wide
        model = getattr(engine._local, "model", None)
        if model is None:
            model = engine._local.model = engine._build(engine.threads)
        return model

    def __exit__(self, *exc):
        if self._locked:
            self._engine._wide_lock.release()
        return False


class TesseractEngine(Engine):
    name = "tesseract"

    TIMEOUT = 90  # seconds per page; a stuck process must not hold a worker for ever

    def __init__(self, threads: int = 1, langs: str = "eng"):
        super().__init__(threads)
        self.langs = langs
        # Tesseract's own OpenMP threads fight each other and the page workers: a page can take
        # a hundred times longer. One thread per page is both faster and predictable.
        os.environ.setdefault("OMP_THREAD_LIMIT", "1")

    @classmethod
    def available(cls) -> bool:
        return bool(importlib.util.find_spec("pytesseract")) and shutil.which("tesseract") is not None

    def recognize(self, image: np.ndarray) -> list[Line]:
        import pytesseract

        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        try:
            data = pytesseract.image_to_data(
                rgb, lang=self.langs, config="--oem 1 --psm 3", output_type=pytesseract.Output.DICT,
                timeout=self.TIMEOUT,
            )
        except (pytesseract.TesseractError, RuntimeError) as exc:  # RuntimeError: timed out
            log.warning("Tesseract failed: %s", exc)
            return []
        grouped: dict[tuple, list[int]] = {}
        for index, word in enumerate(data["text"]):
            if word.strip() and float(data["conf"][index]) >= 0:
                key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
                grouped.setdefault(key, []).append(index)
        lines = []
        for indexes in grouped.values():
            words = [data["text"][i].strip() for i in indexes]
            lengths = [len(w) for w in words]
            conf = sum(float(data["conf"][i]) * n for i, n in zip(indexes, lengths)) / (100 * sum(lengths))
            x0 = min(data["left"][i] for i in indexes)
            y0 = min(data["top"][i] for i in indexes)
            x1 = max(data["left"][i] + data["width"][i] for i in indexes)
            y1 = max(data["top"][i] + data["height"][i] for i in indexes)
            lines.append(Line(" ".join(words), conf, x0, y0, x1, y1))
        return lines

    def orientation(self, image: np.ndarray) -> int | None:
        import pytesseract

        try:
            osd = pytesseract.image_to_osd(cv2.cvtColor(image, cv2.COLOR_BGR2RGB),
                                           output_type=pytesseract.Output.DICT, timeout=self.TIMEOUT)
        except (pytesseract.TesseractError, RuntimeError):  # too little text to decide, or timed out
            return None
        if float(osd.get("orientation_conf", 0)) < 2:
            return None
        return int(osd.get("rotate", 0)) % 360


class EasyOCREngine(Engine):
    name = "easyocr"
    _reader = None
    _lock = threading.Lock()

    @classmethod
    def available(cls) -> bool:
        return bool(importlib.util.find_spec("easyocr"))

    def recognize(self, image: np.ndarray) -> list[Line]:
        with self._lock:  # one shared PyTorch model; it is not safe to call concurrently
            if EasyOCREngine._reader is None:
                import easyocr

                EasyOCREngine._reader = easyocr.Reader(["en"], gpu=False, verbose=False)
            found = EasyOCREngine._reader.readtext(image, paragraph=False)
        return [_line_from_quad(text, conf, quad) for quad, text, conf in found if text.strip()]


ENGINES: dict[str, type[Engine]] = {
    RapidOCREngine.name: RapidOCREngine,
    EasyOCREngine.name: EasyOCREngine,
    TesseractEngine.name: TesseractEngine,
}


def build_engines(names: tuple[str, ...], threads: int = 1, tesseract_langs: str = "eng") -> list[Engine]:
    """Instantiate the installed engines, in the configured order."""
    engines: list[Engine] = []
    for name in names:
        cls = ENGINES.get(name)
        if cls is None:
            log.warning("Unknown OCR engine %r ignored", name)
        elif not cls.available():
            log.info("OCR engine %s is not installed; skipping it", name)
        elif cls is TesseractEngine:
            engines.append(TesseractEngine(threads, tesseract_langs))
        else:
            engines.append(cls(threads))
    return engines


# ── geometry of detected text boxes ──────────────────────────────────────
def _quad_sides(quad: Quad) -> tuple[float, float, float]:
    """(length along the reading direction, height, angle of the top edge in degrees)."""
    top = quad[1] - quad[0]
    left = quad[3] - quad[0]
    return float(np.hypot(*top)), float(np.hypot(*left)), math.degrees(math.atan2(top[1], top[0]))


def crop_line(image: np.ndarray, quad: Quad) -> np.ndarray:
    """Cut a text line out of the page and level it, whatever its tilt."""
    length, height, _ = _quad_sides(quad)
    width, rows = max(int(round(length)), 2), max(int(round(height)), 2)
    target = np.array([[0, 0], [width, 0], [width, rows], [0, rows]], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(quad.astype(np.float32), target)
    crop = cv2.warpPerspective(image, matrix, (width, rows), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)
    return np.rot90(crop).copy() if rows >= 1.5 * width else crop  # a vertical label


def sideways_share(quads: list[Quad]) -> float:
    """Share of text (by box area) whose lines run vertically: near 1 on a page lying on its side."""
    vertical = total = 0.0
    for quad in quads:
        xs, ys = quad[:, 0], quad[:, 1]
        width, height = float(xs.max() - xs.min()), float(ys.max() - ys.min())
        if max(width, height) < 2.0 * min(width, height):
            continue  # short boxes (single words, numbers) say nothing about direction
        total += width * height
        if height > width:
            vertical += width * height
    return vertical / total if total else 0.0


def skew_angle(quads: list[Quad]) -> float:
    """Median tilt of the longer text lines in degrees; positive when lines descend to the right."""
    angles = []
    for quad in quads:
        length, height, angle = _quad_sides(quad)
        if length > 4 * height:
            angles.append(((angle + 90) % 180) - 90)
    return float(np.median(angles)) if len(angles) >= 3 else 0.0


def longest(quads: list[Quad], limit: int = 6) -> list[Quad]:
    """The longest clearly horizontal lines: the ones used to tell which way up the page is."""
    wide = [q for q in quads if _quad_sides(q)[1] >= 8 and _quad_sides(q)[0] > 3 * _quad_sides(q)[1]]
    return sorted(wide, key=lambda q: -_quad_sides(q)[0])[:limit]


def flip_quads(quads: list[Quad], width: int, height: int) -> list[Quad]:
    """The same boxes after the page was turned by 180 degrees."""
    size = np.array([width - 1, height - 1], dtype=np.float32)
    return [np.roll(size - quad, 2, axis=0) for quad in quads]


def level_quads(quads: list[Quad], angle: float, width: int, height: int) -> list[Quad]:
    """Box coordinates as they would be on a deskewed page (used for the reading order only)."""
    if not angle:
        return quads
    radians = math.radians(angle)
    cos, sin = math.cos(radians), math.sin(radians)
    turn = np.array([[cos, -sin], [sin, cos]], dtype=np.float32)  # row vectors: p' = p @ turn
    centre = np.array([width / 2, height / 2], dtype=np.float32)
    return [(quad - centre) @ turn + centre for quad in quads]


def line_from_quad(text: str, conf: float, quad: Quad) -> Line:
    return _line_from_quad(text, conf, quad)
