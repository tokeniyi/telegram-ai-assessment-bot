"""Image clean-up before recognition. Everything works on OpenCV BGR arrays.

Two variants of a page are produced on demand:
- the *normal* page: right way up, deskewed, large enough to read. Neural OCR
  models are trained on natural images and read this best when it is clean.
- the *enhanced* page: shadows flattened, contrast stretched, noise removed,
  edges sharpened. Used for a second attempt when the first one is unsure.
"""

from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_PIXELS = 80_000_000          # refuse decompression bombs
MAX_FRAMES = 30                  # pages read from a multi-page TIFF / animated image
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

try:  # iPhone photos sent as files are HEIC; supported when pillow-heif is installed
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass


class ImageError(Exception):
    """The bytes are not an image that can be read; the message is safe to show."""


# ── loading ──────────────────────────────────────────────────────────────
def decode_images(data: bytes) -> list[np.ndarray]:
    """Decode image bytes into one BGR array per frame, honouring EXIF rotation."""
    try:
        image = Image.open(io.BytesIO(data))
        frames = []
        for index in range(min(getattr(image, "n_frames", 1), MAX_FRAMES)):
            image.seek(index)
            frame = ImageOps.exif_transpose(image)
            if frame.mode in ("RGBA", "LA", "P"):  # flatten transparency onto white paper
                frame = frame.convert("RGBA")
                paper = Image.new("RGBA", frame.size, "white")
                frame = Image.alpha_composite(paper, frame)
            frames.append(cv2.cvtColor(np.asarray(frame.convert("RGB")), cv2.COLOR_RGB2BGR))
        return frames
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError) as exc:
        raise ImageError(f"That image could not be opened ({exc}).") from exc


def encode_jpeg(image: np.ndarray, max_side: int = 1568, quality: int = 82) -> bytes:
    ok, buffer = cv2.imencode(".jpg", limit_side(image, max_side), [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ImageError("The page image could not be encoded.")
    return buffer.tobytes()


def limit_side(image: np.ndarray, max_side: int) -> np.ndarray:
    height, width = image.shape[:2]
    scale = max_side / max(height, width)
    if scale >= 1:
        return image
    return cv2.resize(image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)


def to_gray(image: np.ndarray) -> np.ndarray:
    return image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


# ── measurements ─────────────────────────────────────────────────────────
def ink_mask(gray: np.ndarray) -> np.ndarray:
    """255 where there is ink, whatever the lighting: adaptive threshold on a blurred page."""
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    block = max(15, (min(gray.shape) // 40) | 1)
    return cv2.adaptiveThreshold(blurred, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, block, 12)


def is_blank(image: np.ndarray) -> bool:
    """True for an empty page (no text and no drawing)."""
    gray = to_gray(limit_side(image, 1000))
    smooth = cv2.GaussianBlur(gray, (5, 5), 0)  # so that single noisy pixels do not count as contrast
    if int(smooth.max()) - int(smooth.min()) < 25:
        return True
    # one short word on an otherwise empty page is about 0.05% ink; dust and noise are far less
    return float(np.count_nonzero(ink_mask(gray))) / gray.size < 0.0003


def noise_level(gray: np.ndarray) -> float:
    """Estimated sigma of pixel noise (Immerkaer's fast method)."""
    kernel = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float32)
    response = cv2.filter2D(gray.astype(np.float32), -1, kernel)
    height, width = gray.shape
    return float(np.sqrt(np.pi / 2) * np.abs(response[1:-1, 1:-1]).sum() / (6 * (width - 2) * (height - 2)))


# ── geometry ─────────────────────────────────────────────────────────────
def rotate_quarter(image: np.ndarray, degrees_clockwise: int) -> np.ndarray:
    codes = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    turn = degrees_clockwise % 360
    return image if turn == 0 else cv2.rotate(image, codes[turn])


def rotate(image: np.ndarray, angle: float) -> np.ndarray:
    """Rotate counter-clockwise by a small angle, growing the canvas so nothing is cut off."""
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_w, new_h = int(height * sin + width * cos), int(height * cos + width * sin)
    matrix[0, 2] += new_w / 2 - width / 2
    matrix[1, 2] += new_h / 2 - height / 2
    return cv2.warpAffine(image, matrix, (new_w, new_h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def estimate_skew(image: np.ndarray, limit: float = 15.0) -> float:
    """Angle in degrees (counter-clockwise positive) that would make the text lines level.

    Projection-profile method: text lines produce the sharpest row histogram when
    they are horizontal. Returns 0 when no angle is clearly better than leaving it.
    """
    gray = to_gray(limit_side(image, 900))
    mask = ink_mask(gray)
    if np.count_nonzero(mask) < 200:
        return 0.0

    def sharpness(angle: float) -> float:
        height, width = mask.shape
        matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
        rows = cv2.warpAffine(mask, matrix, (width, height), flags=cv2.INTER_NEAREST).sum(axis=1, dtype=np.float64)
        return float(((rows[1:] - rows[:-1]) ** 2).sum())

    best, best_score = 0.0, sharpness(0.0)
    baseline = best_score
    for step in (1.0, 0.25):
        centre = best
        for angle in np.arange(centre - (limit if step == 1.0 else 1.0), centre + (limit if step == 1.0 else 1.0) + 1e-6, step):
            score = sharpness(float(angle))
            if score > best_score:
                best, best_score = float(angle), score
    if abs(best) < 0.3 or best_score < baseline * 1.15:
        return 0.0
    return best


def upscale(image: np.ndarray, factor: float) -> np.ndarray:
    """Enlarge small text. Cubic interpolation plus a light sharpen keeps strokes crisp."""
    height, width = image.shape[:2]
    enlarged = cv2.resize(image, (round(width * factor), round(height * factor)), interpolation=cv2.INTER_CUBIC)
    return sharpen(enlarged, amount=0.4)


# ── enhancement ──────────────────────────────────────────────────────────
def flatten_illumination(gray: np.ndarray) -> np.ndarray:
    """Remove shadows and uneven lighting by dividing the page by its estimated background."""
    scale = 600 / max(gray.shape)
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (25, 25))
    background = cv2.medianBlur(cv2.morphologyEx(small, cv2.MORPH_CLOSE, kernel), 21)
    background = cv2.resize(background, (gray.shape[1], gray.shape[0]), interpolation=cv2.INTER_LINEAR)
    flat = cv2.divide(gray, background, scale=255)
    return flat


def sharpen(image: np.ndarray, amount: float = 0.8) -> np.ndarray:
    blurred = cv2.GaussianBlur(image, (0, 0), 1.5)
    return cv2.addWeighted(image, 1 + amount, blurred, -amount, 0)


def enhance(image: np.ndarray) -> np.ndarray:
    """The second-attempt page: flat lighting, denoised, local contrast stretched, sharpened."""
    gray = to_gray(image)
    sigma = noise_level(gray)
    if sigma > 4:  # camera noise or heavy JPEG compression
        gray = cv2.fastNlMeansDenoising(gray, None, h=min(3 + sigma, 14), templateWindowSize=7, searchWindowSize=15)
    gray = flatten_illumination(gray)
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    gray = sharpen(gray, amount=0.6)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


# ── figures ──────────────────────────────────────────────────────────────
def has_figure(image: np.ndarray, text_boxes: list[tuple[float, float, float, float]]) -> bool:
    """True when the page has a sizeable drawing, chart or photo outside the text lines."""
    height, width = image.shape[:2]
    scale = 800 / max(height, width)
    gray = to_gray(cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else image)
    scale = min(scale, 1.0)
    mask = ink_mask(gray)
    for x0, y0, x1, y1 in text_boxes:  # erase the text so only drawings remain
        pad = 3
        cv2.rectangle(mask, (int(x0 * scale) - pad, int(y0 * scale) - pad),
                      (int(x1 * scale) + pad, int(y1 * scale) + pad), 0, -1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9)))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    page_area = mask.shape[0] * mask.shape[1]
    for index in range(1, count):
        w, h, area = stats[index, cv2.CC_STAT_WIDTH], stats[index, cv2.CC_STAT_HEIGHT], stats[index, cv2.CC_STAT_AREA]
        box_area = w * h
        # a drawing spans a real region in both directions; ruled lines and borders do not
        if box_area > 0.03 * page_area and min(w, h) > 0.08 * min(mask.shape) and area > 0.02 * box_area:
            if w > 0.97 * mask.shape[1] and h > 0.97 * mask.shape[0]:
                continue  # the page border itself
            return True
    return False
