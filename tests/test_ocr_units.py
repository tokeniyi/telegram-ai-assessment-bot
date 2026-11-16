"""OCR building blocks that need no OCR engine: file detection, clean-up, reading order, AI guards."""

import io
import json

import pytest

from bot.ai import AIError
from bot.config import ConfigError, load_settings
from bot.ocr import language_model as lm
from bot.ocr.detector import file_kind, text_layer_quality
from bot.ocr.layout import page_text
from bot.ocr.postprocess import clean_page, looks_like_math, plausibility, strip_running_lines
from bot.ocr.types import DocumentResult, Line, PageResult
from tests import ocr_samples as samples
from tests.conftest import FakeProvider, FakeVisionProvider


# ── stage 1: what is this file, does it need OCR ─────────────────────────
def test_file_kind_trusts_content_over_name():
    assert file_kind("notes.txt", b"%PDF-1.7 ...") == "pdf"
    assert file_kind("scan.bin", b"\xff\xd8\xff\xe0rest") == "image"
    assert file_kind("page.PNG", b"") == "image"
    assert file_kind(None, b"", "image/webp") == "image"
    assert file_kind("notes.docx", b"PK\x03\x04") == "other"


def test_text_layer_quality():
    assert text_layer_quality("") == "none"
    assert text_layer_quality("   \n 12 \n") == "none"                       # just a page number
    assert text_layer_quality("Scanned with CamScanner " * 2) == "sparse"   # a stamp on a scanned page
    assert text_layer_quality("Cells are the basic unit of life. " * 10) == "good"
    assert text_layer_quality("��� a1 �� b2 " * 30) == "sparse"  # broken font encoding


# ── clean-up ─────────────────────────────────────────────────────────────
def test_clean_page_repairs_safe_errors_only():
    raw = "The ﬁrst  cell   divides by mito-\nsis into two ce11s.\n. '\nThe p0werhouse makes ATP.\n\n\n\nx1 + 10 = 0"
    assert clean_page(raw) == (
        "The first cell divides by mitosis\ninto two cells.\nThe powerhouse makes ATP.\n\nx1 + 10 = 0"
    )
    assert clean_page("the m1tochondria") == "the m1tochondria"  # 1 could be l or i: left for the AI layer
    assert clean_page("H2O and CO2 at 100 C, see B12") == "H2O and CO2 at 100 C, see B12"  # real digits stay


def test_math_detection_and_plausibility():
    assert looks_like_math("Solve the equation\nx^2 + 3x + 2 = 0\ny = 2x + 1")
    assert not looks_like_math("The heart pumps blood.\nIt has four chambers.\nValves stop backflow.")
    assert plausibility("The heart pumps blood around the body.") > 0.9
    assert plausibility("|| ~~ ¬¬ ]] ^^ `` ;; __") < 0.3


def test_running_headers_and_page_numbers_are_removed():
    pages = [f"Biology 101 - Chapter 3\nBody of page {n} talks about topic {n}.\nMore text here.\n{n}" for n in range(1, 7)]
    cleaned = strip_running_lines(pages)
    assert all("Biology 101" not in page for page in cleaned)
    assert cleaned[2] == "Body of page 3 talks about topic 3.\nMore text here."
    assert strip_running_lines(["1. B\n2"]) == ["1. B\n2"]  # a short page: a bare number may be content


# ── reading order ────────────────────────────────────────────────────────
def _line(text, x, y, width, height=30):
    return Line(text, 0.99, x, y, x + width, y + height)


def test_two_columns_under_a_full_width_title_are_read_one_after_the_other():
    lines = [_line("A title across the page", 300, 40, 900, 46)]
    for row in range(4):  # the engine returns lines row by row, mixing the columns
        lines.append(_line(f"left column sentence number {row}", 60, 150 + row * 45, 560))
        lines.append(_line(f"right column sentence number {row}", 800, 150 + row * 45, 560))
    assert page_text(lines).split("\n") == [
        "A title across the page", "",
        *[f"left column sentence number {row}" for row in range(4)], "",
        *[f"right column sentence number {row}" for row in range(4)],
    ]


def test_table_is_read_row_by_row_with_cells_kept_apart():
    lines = []
    for row, (name, role) in enumerate([("Organelle", "Function"), ("Nucleus", "Stores the DNA"),
                                        ("Ribosome", "Builds proteins"), ("Chloroplast", "Makes glucose")]):
        lines.append(_line(name, 80, 60 + row * 70, 14 * len(name)))
        lines.append(_line(role, 640, 60 + row * 70, 14 * len(role)))
    assert page_text(lines) == ("Organelle | Function\nNucleus | Stores the DNA\n"
                                "Ribosome | Builds proteins\nChloroplast | Makes glucose")


def test_paragraph_breaks_and_headings():
    lines = [_line("Heading", 60, 40, 300, 52), _line("first line", 60, 120, 800), _line("second line", 60, 160, 800),
             _line("new paragraph", 60, 260, 800), _line("goes on", 60, 300, 800)]
    assert page_text(lines) == "Heading\n\nfirst line\nsecond line\n\nnew paragraph\ngoes on"
    assert page_text([]) == ""


# ── AI layer guards ──────────────────────────────────────────────────────
OCR_TEXT = ("Thc m1tochondria is thc powcrhousc of thc ccll. It rcleases encrgy from glucosc "
            "during acrobic rcspiration and storcs it as ATP molcculcs.")
FIXED = ("The mitochondria is the powerhouse of the cell. It releases energy from glucose "
         "during aerobic respiration and stores it as ATP molecules.")


async def test_correction_is_applied_when_it_stays_close_to_the_ocr_text():
    provider = FakeProvider()
    provider.replies = [json.dumps({"text": FIXED})]
    text, changed = await lm.correct_text(provider, OCR_TEXT)
    assert (text, changed) == (FIXED, True)
    assert "<ocr>" in provider.calls[0][1]


async def test_correction_that_rewrites_or_invents_is_rejected():
    provider = FakeProvider()
    provider.replies = [
        json.dumps({"text": "Mitochondria are organelles. Here is a summary of cellular respiration in three steps..."}),
        json.dumps({"text": FIXED + " " + "Additionally, chloroplasts perform photosynthesis in plant cells. " * 3}),
        "not json at all",
    ]
    for _ in range(3):
        assert await lm.correct_text(provider, OCR_TEXT) == (OCR_TEXT, False)


async def test_correction_splits_long_text_into_chunks():
    provider = FakeProvider()
    text = "\n\n".join(f"Paragraph {n}. " + " ".join(["word"] * 400) for n in range(8))
    result, changed = await lm.correct_text(provider, text)  # the fake returns each chunk unchanged
    assert result == text and not changed and len(provider.calls) > 1
    assert all(len(user) < lm.CHUNK_CHARS + 500 for _, user in provider.calls)


async def test_vision_transcription_must_agree_with_confident_ocr():
    confident = "Photosynthesis converts light energy into chemical energy inside the chloroplasts of plant cells"
    provider = FakeVisionProvider()
    provider.vision_replies = [
        json.dumps({"text": "Photosynthesis converts light energy into chemical energy inside the chloroplasts "
                            "of plant cells.\n$6CO_2 + 6H_2O \\\\to C_6H_{12}O_6 + 6O_2$\n[Figure: a leaf]"}),
        json.dumps({"text": "The French Revolution began in 1789 with the storming of the Bastille in Paris."}),
        "I cannot see any image.",
    ]
    good = await lm.read_page(provider, b"jpeg", "ocr draft", confident, model="vision-model")
    assert "$6CO_2" in good and "[Figure: a leaf]" in good
    assert provider.vision_calls[0][2] == [b"jpeg"] and provider.vision_calls[0][3] == "vision-model"
    assert await lm.read_page(provider, b"jpeg", "ocr draft", confident) is None   # made up
    assert await lm.read_page(provider, b"jpeg", "ocr draft", confident) is None   # not JSON
    # handwriting local OCR could not read at all: nothing to check against, so it is accepted
    provider.vision_replies = [json.dumps({"text": "1. B  2. osmosis"})]
    assert await lm.read_page(provider, b"jpeg", "", "") == "1. B  2. osmosis"


def test_supports_vision():
    assert lm.supports_vision(FakeVisionProvider()) and not lm.supports_vision(FakeProvider())


# ── result object ────────────────────────────────────────────────────────
def test_document_result_report_and_confidence():
    document = DocumentResult(kind="pdf", pages=[
        PageResult(1, "typed text " * 5, "text"),
        PageResult(2, "x" * 100, "ocr", confidence=0.9, engine="rapidocr"),
        PageResult(3, "y" * 300, "ocr", confidence=0.5, engine="tesseract+enhance"),
        PageResult(4),
    ])
    assert document.used_ocr and len(document.ocr_pages) == 2
    assert document.confidence == pytest.approx(0.6)  # weighted by text length
    assert document.report()[2] == {"page": 3, "source": "ocr", "engine": "tesseract+enhance",
                                    "confidence": 0.5, "chars": 300, "corrected": False}
    assert document.pages[2].low_confidence(0.8) and not document.pages[0].low_confidence(0.8)


# ── settings ─────────────────────────────────────────────────────────────
OCR_KEYS = ["OCR_ENABLED", "OCR_ENGINES", "OCR_WORKERS", "OCR_MIN_CONFIDENCE", "OCR_VISION", "OCR_AI_CORRECTION",
            "OCR_VISION_MODEL", "OCR_MAX_PAGES", "TELEGRAM_BOT_TOKEN", "AI_API_KEY", "AI_PROVIDER", "ADMIN_IDS",
            "AI_MODEL", "AI_BASE_URL"]


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    import os

    for key in OCR_KEYS:
        monkeypatch.delenv(key, raising=False)

    def write(text):
        path = tmp_path / ".env"
        path.write_text("TELEGRAM_BOT_TOKEN=1:abc\nAI_API_KEY=k\n" + text)
        return path

    yield write
    for key in OCR_KEYS:
        os.environ.pop(key, None)


def test_ocr_settings_defaults_and_overrides(env_file):
    defaults = load_settings(env_file("")).ocr
    assert defaults.enabled and defaults.engines == ("rapidocr", "tesseract")
    assert (defaults.vision, defaults.ai_correction, defaults.min_confidence) == ("auto", "auto", 0.8)

    custom = load_settings(env_file(
        "OCR_ENGINES=tesseract, rapidocr\nOCR_WORKERS=2\nOCR_MIN_CONFIDENCE=90\nOCR_VISION=off\n"
        "OCR_AI_CORRECTION=true\nOCR_VISION_MODEL=some/vision-model\nOCR_MAX_PAGES=40\n"
    )).ocr
    assert custom.engines == ("tesseract", "rapidocr") and custom.workers == 2 and custom.max_pages == 40
    assert (custom.min_confidence, custom.vision, custom.ai_correction) == (0.9, "off", "on")
    assert custom.vision_model == "some/vision-model"


@pytest.mark.parametrize("line", ["OCR_ENGINES=google-vision", "OCR_VISION=sometimes", "OCR_MIN_CONFIDENCE=250"])
def test_ocr_settings_reject_bad_values(env_file, line):
    with pytest.raises(ConfigError):
        load_settings(env_file(line + "\n"))


# ── image preparation (needs OpenCV, no OCR engine) ──────────────────────
cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
from PIL import Image  # noqa: E402

from bot.ocr import preprocess as pp  # noqa: E402
from bot.ocr import recognizer as rec  # noqa: E402


def test_decode_images_applies_exif_rotation_and_rejects_junk():
    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6  # the camera was held sideways: rotate 90 degrees clockwise to view
    Image.new("RGB", (400, 200), "white").save(buffer, "JPEG", exif=exif)
    (frame,) = pp.decode_images(buffer.getvalue())
    assert frame.shape[:2] == (400, 200)

    transparent = Image.new("RGBA", (50, 50), (0, 0, 0, 0))  # transparent means paper, not black
    (frame,) = pp.decode_images(samples.png(transparent))
    assert frame.min() == 255
    with pytest.raises(pp.ImageError):
        pp.decode_images(b"\xff\xd8\xff this is not really a jpeg")


def test_blank_page_detection():
    assert pp.is_blank(samples.bgr(Image.new("RGB", (800, 600), "white")))
    assert pp.is_blank(samples.bgr(Image.new("RGB", (800, 600), (40, 40, 40))))
    assert not pp.is_blank(samples.bgr(samples.page()))
    assert not pp.is_blank(samples.bgr(samples.page(["1. B"])))  # one short line on a big page is not blank
    noisy = np.clip(np.random.default_rng(1).normal(235, 6, (600, 800, 3)), 0, 255).astype(np.uint8)
    assert pp.is_blank(noisy)  # an empty sheet photographed with sensor noise
    assert not pp.is_blank(samples.bgr(samples.photographed(samples.page())))


@pytest.mark.parametrize("tilt", [-9, -3, 4, 11])
def test_estimate_skew_finds_the_tilt(tilt):
    tilted = samples.page().rotate(tilt, expand=True, fillcolor="white", resample=Image.BICUBIC)
    angle = pp.estimate_skew(samples.bgr(tilted))
    assert angle == pytest.approx(-tilt, abs=0.6)
    assert abs(pp.estimate_skew(pp.rotate(samples.bgr(tilted), angle))) < 0.6  # level after correction
    assert pp.estimate_skew(samples.bgr(samples.page())) == 0.0


def test_enhance_flattens_a_shadow():
    photo = samples.bgr(samples.photographed(samples.page()))

    def paper_spread(image):  # brightness difference of the paper between the lit and the shadowed side
        gray = pp.to_gray(image).astype(float)
        columns = np.percentile(gray, 90, axis=0)
        return columns[:200].mean() - columns[-200:].mean()

    assert paper_spread(photo) > 60
    assert abs(paper_spread(pp.enhance(photo))) < 15
    assert pp.noise_level(pp.to_gray(pp.enhance(photo))) < pp.noise_level(pp.to_gray(photo))


def test_figure_detection_ignores_text_and_finds_drawings():
    text_only = samples.bgr(samples.page())
    assert not pp.has_figure(text_only, [])  # text lines alone are not a figure
    diagram = samples.bgr(samples.with_diagram())
    assert pp.has_figure(diagram, [(60, 50, 1250, 200)])
    assert not pp.has_figure(samples.bgr(Image.new("RGB", (900, 700), "white")), [])


def test_quad_geometry_helpers():
    def quad(x, y, w, h):
        return np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float32)

    upright = [quad(50, 50 + 60 * i, 600, 30) for i in range(5)]
    assert rec.sideways_share(upright) == 0.0 and rec.skew_angle(upright) == 0.0
    sideways = [quad(50 + 60 * i, 50, 30, 600) for i in range(5)]
    assert rec.sideways_share(sideways) == 1.0

    slope = np.tan(np.radians(6))
    tilted = [np.array([[50, y], [650, y + 600 * slope], [650, y + 600 * slope + 30], [50, y + 30]], dtype=np.float32)
              for y in (50, 110, 170, 230)]
    assert rec.skew_angle(tilted) == pytest.approx(6, abs=0.1)
    levelled = rec.level_quads(tilted, 6, 800, 400)
    assert all(abs(q[1][1] - q[0][1]) < 1 for q in levelled)  # top edges are horizontal again

    flipped = rec.flip_quads([quad(10, 20, 100, 30)], 400, 300)[0]
    assert flipped[0].tolist() == [289, 249] and flipped[2].tolist() == [389, 279]  # still TL .. BR order

    crop = rec.crop_line(samples.bgr(samples.page()), quad(60, 50, 900, 50))
    assert crop.shape[:2] == (50, 900)
