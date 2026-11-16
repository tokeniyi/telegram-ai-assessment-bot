"""The OCR pipeline on real images: accuracy on damaged pages, PDFs, fallbacks, the AI layer."""

import io
import json

import pytest

pytest.importorskip("cv2")
pytest.importorskip("rapidocr")
pytest.importorskip("pypdfium2")

from PIL import Image  # noqa: E402

from bot.ai import AIError  # noqa: E402
from bot.extract import ExtractionError  # noqa: E402
from bot.ocr import OCRConfig, OCRService, describe_result  # noqa: E402
from bot.ocr.recognizer import EasyOCREngine, Engine, TesseractEngine  # noqa: E402
from bot.ocr.types import Line  # noqa: E402
from tests import ocr_samples as samples  # noqa: E402
from tests.conftest import FakeProvider, FakeVisionProvider  # noqa: E402

PAGE = samples.page()
TEXT = " ".join(samples.PARAGRAPH)


def tilted(image, degrees):
    return image.rotate(degrees, expand=True, fillcolor="white", resample=Image.BICUBIC)


@pytest.fixture(scope="module")
def ocr():
    service = OCRService(OCRConfig(engines=("rapidocr",), workers=2))
    yield service
    service.shutdown()


def make_service(provider=None, **config):
    config.setdefault("engines", ("rapidocr",))
    config.setdefault("workers", 2)
    return OCRService(OCRConfig(**config), get_provider=lambda: provider)


# ── images: every kind of damage a real upload has ───────────────────────
CASES = {
    "clean scan": PAGE,
    "lying on its side (90)": PAGE.rotate(90, expand=True),
    "upside down (180)": PAGE.rotate(180, expand=True),
    "lying on its side (270)": PAGE.rotate(270, expand=True),
    "tilted 7 degrees": tilted(PAGE, 7),
    "tilted -11 degrees": tilted(PAGE, -11),
    "phone photo: shadow, blur, noise": samples.photographed(PAGE),
    "phone photo, tilted": samples.photographed(tilted(PAGE, 5)),
    "low resolution": PAGE.resize((520, 280), Image.LANCZOS),
    "heavy JPEG compression": Image.open(io.BytesIO(samples.jpeg(PAGE, quality=12))),
}


@pytest.mark.parametrize("name", CASES)
async def test_reads_damaged_pages_accurately(ocr, name):
    document = await ocr.extract("page.png", samples.png(CASES[name]))
    page = document.pages[0]
    assert samples.accuracy(TEXT, document.text) >= 0.98, document.text
    assert page.source == "ocr" and page.confidence >= 0.9 and document.warnings == []
    # lines come out in reading order even when the page arrived rotated
    assert document.text.index("Photosynthesis") < document.text.index("Calvin") < document.text.index("Osmosis")


async def test_columns_are_not_interleaved(ocr):
    document = await ocr.extract("notes.jpg", samples.jpeg(samples.two_columns()))
    lines = [line for line in document.text.split("\n") if line]
    assert lines == ["Circulation of the blood in humans", *samples.LEFT_COLUMN, *samples.RIGHT_COLUMN]


async def test_table_rows_keep_their_cells_together(ocr):
    document = await ocr.extract("table.png", samples.png(samples.table()))
    assert "Nucleus | Stores the DNA" in document.text and "Organelle | Function" in document.text


async def test_formulas_and_figures_are_flagged(ocr):
    formula = samples.page(["Solve the quadratic equation:", "x^2 + 3x + 2 = 0", "y = 2x + 1"])
    page = (await ocr.extract("f.png", samples.png(formula))).pages[0]
    assert page.has_math and "3x" in page.text
    page = (await ocr.extract("d.png", samples.png(samples.with_diagram()))).pages[0]
    assert page.has_figure and not page.has_math
    assert not (await ocr.extract("p.png", samples.png(PAGE))).pages[0].has_figure


async def test_blank_and_broken_images_give_a_clear_error(ocr):
    with pytest.raises(ExtractionError, match="No readable text"):
        await ocr.extract("blank.png", samples.png(Image.new("RGB", (800, 600), "white")))
    with pytest.raises(ExtractionError, match="could not be opened"):
        await ocr.extract("broken.jpg", b"\xff\xd8\xff not an image")


async def test_album_of_images_is_one_document_in_order(ocr):
    first = samples.page(["Page one is about the cell membrane."])
    second = samples.page(["Page two is about the cell nucleus."])
    document = await ocr.extract_images([samples.jpeg(first), samples.jpeg(second)])
    assert [p.number for p in document.pages] == [1, 2]
    assert document.text.index("membrane") < document.text.index("nucleus")
    assert describe_result(document).startswith("🔎 Text read from 2 images")


# ── PDFs ─────────────────────────────────────────────────────────────────
async def test_mixed_pdf_uses_the_text_layer_where_there_is_one(ocr):
    typed = ["Enzymes are biological catalysts that speed up reactions."] * 6
    pdf = samples.merge_pdfs(
        samples.text_pdf([typed]),
        samples.scanned_pdf([PAGE, samples.two_columns(), Image.new("RGB", (800, 600), "white")]),
    )
    seen = []

    async def progress(stage, done, total):
        seen.append((stage, done, total))

    document = await ocr.extract("notes.pdf", pdf, progress=progress)
    assert [p.source for p in document.pages] == ["text", "ocr", "ocr", "empty"]
    assert document.pages[0].confidence is None and "Enzymes are biological catalysts" in document.pages[0].text
    assert samples.accuracy(TEXT, document.pages[1].text) >= 0.98
    assert "Capillaries link the two." in document.pages[2].text
    assert document.confidence >= 0.9 and document.warnings == []
    assert seen[0] == ("ocr", 0, 3) and seen[-1] == ("ocr", 3, 3)  # only the three scanned pages were OCRed
    assert describe_result(document).startswith("🔎 4 pages: 1 with text, 2 scanned and read by OCR")
    assert [r["page"] for r in document.report()] == [1, 2, 3, 4]


async def test_text_pdf_never_touches_the_ocr_engine():
    service = make_service()
    service._engines = [ExplodingEngine()]
    document = await service.extract("typed.pdf", samples.text_pdf([["Osmosis is the diffusion of water."] * 8]))
    assert not document.used_ocr and "Osmosis" in document.text and describe_result(document) is None


async def test_scan_with_only_a_stamp_as_text_is_still_ocred(ocr):
    """Scanner apps add a line of real text to an otherwise pictured page."""
    stamp = samples.text_pdf([["Scanned with CamScanner - www.camscanner.example"]])
    scan = samples.scanned_pdf([PAGE])
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=io.BytesIO(scan))
    writer.pages[0].merge_page(PdfReader(io.BytesIO(stamp)).pages[0])
    buffer = io.BytesIO()
    writer.write(buffer)

    document = await ocr.extract("scan.pdf", buffer.getvalue())
    assert document.pages[0].source == "ocr" and "Photosynthesis" in document.text


async def test_pages_are_processed_in_parallel_and_stay_in_order(ocr):
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
    pages = [samples.page([f"This is the page about {word}.", "It has two lines of text."], size=(900, 300))
             for word in words]
    document = await ocr.extract("long.pdf", samples.scanned_pdf(pages))
    assert len(document.pages) == 8
    for page, word in zip(document.pages, words):
        assert word in page.text.lower() and page.source == "ocr"


async def test_page_limit_is_enforced_and_reported(ocr):
    pages = [samples.page([f"Page number {n} of the scan."], size=(900, 200)) for n in range(1, 4)]
    document = await ocr.extract("long.pdf", samples.scanned_pdf(pages), max_pages=2)
    assert [p.source for p in document.pages] == ["ocr", "ocr", "empty"]
    assert "only the first 2 were read" in document.warnings[0]


async def test_bad_pdfs():
    service = make_service()
    with pytest.raises(ExtractionError, match="could not be read"):
        await service.extract("a.pdf", b"%PDF-1.4 broken")


async def test_scanned_pdf_without_ocr_explains_why():
    scan = samples.scanned_pdf([PAGE])
    with pytest.raises(ExtractionError, match="switched off"):
        await make_service(enabled=False).extract("scan.pdf", scan)
    typed = await make_service(enabled=False).extract("t.pdf", samples.text_pdf([["Plain typed text. " * 3] * 5]))
    assert "Plain typed text" in typed.text  # text PDFs do not need OCR at all


# ── confidence, retries and fallback engines (deterministic stand-in engines) ──
class StubEngine(Engine):
    """Returns the same lines for every page, with a fixed confidence."""

    def __init__(self, name, text, confidence):
        super().__init__()
        self.name, self._text, self._confidence, self.calls = name, text, confidence, 0

    def recognize(self, image):
        self.calls += 1
        return [Line(line, self._confidence, 50, 50 + 40 * i, 900, 80 + 40 * i)
                for i, line in enumerate(self._text.split("\n"))]


class ExplodingEngine(Engine):
    name = "exploding"

    def recognize(self, image):
        raise AssertionError("the OCR engine must not be called")


GARBLED = "Thc m1tochondria is thc powcrhousc of thc ccll and makcs ATP for it"
CLEAR = "The mitochondria is the powerhouse of the cell and makes ATP for it"


async def test_confident_first_attempt_skips_retries_and_fallbacks():
    service = make_service()
    first, second = StubEngine("first", CLEAR, 0.97), StubEngine("second", "other", 0.99)
    service._engines = [first, second]
    document = await service.extract("p.png", samples.png(PAGE))
    assert (first.calls, second.calls) == (1, 0)
    assert document.pages[0].engine == "first" and document.warnings == []


async def test_unsure_page_is_retried_enhanced_then_passed_to_the_fallback_engine():
    service = make_service()
    weak, strong = StubEngine("weak", GARBLED, 0.55), StubEngine("strong", CLEAR, 0.93)
    service._engines = [weak, strong]
    page = (await service.extract("p.png", samples.png(PAGE))).pages[0]
    assert weak.calls == 2 and strong.calls == 1      # normal page, enhanced page, then the fallback
    assert (page.engine, page.text, page.confidence) == ("strong+enhance", CLEAR, pytest.approx(0.93))


async def test_low_confidence_is_reported_per_page():
    service = make_service()
    service._engines = [StubEngine("weak", GARBLED, 0.55)]
    document = await service.extract("scan.pdf", samples.scanned_pdf([PAGE, PAGE]))
    assert document.warnings == ["Low text confidence on page 1, 2 — the text read there may contain mistakes."]
    assert document.report()[0]["confidence"] == 0.55
    assert "confidence 55%" in describe_result(document)


async def test_lines_the_engine_barely_believes_are_left_out_but_counted():
    class Mixed(Engine):
        name = "mixed"

        def recognize(self, image):
            return [Line(CLEAR, 0.98, 50, 50, 900, 80), Line("~~ |||| xx ;;", 0.2, 50, 100, 400, 130)]

    service = make_service()
    service._engines = [Mixed()]
    page = (await service.extract("p.png", samples.png(PAGE))).pages[0]
    assert page.text == CLEAR and page.confidence < 0.9   # the debris is not in the text, but lowers the score


# ── the AI layer inside the pipeline ─────────────────────────────────────
async def test_low_confidence_page_is_repaired_by_the_text_model():
    provider = FakeProvider()
    provider.replies = [json.dumps({"text": CLEAR})]
    service = make_service(provider)
    service._engines = [StubEngine("weak", GARBLED, 0.55)]
    page = (await service.extract("p.png", samples.png(PAGE))).pages[0]
    assert page.text == CLEAR and page.corrected and page.source == "ocr"

    provider.replies = [json.dumps({"text": "A completely different text about the French Revolution of 1789."})]
    page = (await service.extract("p.png", samples.png(PAGE))).pages[0]
    assert page.text == GARBLED and not page.corrected      # a rewrite is refused


async def test_confident_pages_are_not_sent_to_the_ai(ocr):
    provider = FakeVisionProvider()
    service = make_service(provider)
    document = await service.extract("p.png", samples.png(PAGE))
    assert provider.calls == [] and provider.vision_calls == [] and document.pages[0].source == "ocr"
    service.shutdown()


async def test_page_with_a_diagram_goes_to_the_vision_model():
    provider = FakeVisionProvider()
    transcript = (" ".join(samples.PARAGRAPH[:2]) +
                  "\n[Figure: a circle divided in two with a rectangle inside, labelled chloroplast]")
    provider.vision_replies = [json.dumps({"text": transcript})]
    service = make_service(provider, vision_model="vision-large")
    seen = []

    async def progress(stage, done, total):
        seen.append((stage, done, total))

    document = await service.extract("d.png", samples.png(samples.with_diagram()), progress=progress)
    page = document.pages[0]
    assert page.source == "vision" and "[Figure: a circle" in page.text and page.image is None
    system, user, images, model = provider.vision_calls[0]
    assert images[0][:3] == b"\xff\xd8\xff" and model == "vision-large"   # a JPEG of the page
    assert "Photosynthesis converts" in user                              # the local OCR draft is the hint
    assert ("ai", 1, 1) in seen and "read by the AI vision model" in describe_result(document)
    service.shutdown()


async def test_vision_reply_that_ignores_the_page_is_discarded():
    provider = FakeVisionProvider()
    provider.vision_replies = [json.dumps({"text": "The French Revolution began in 1789 in Paris, France."})]
    service = make_service(provider)
    page = (await service.extract("d.png", samples.png(samples.with_diagram()))).pages[0]
    assert page.source == "ocr" and "Photosynthesis" in page.text
    service.shutdown()


async def test_vision_failure_keeps_local_text_and_tells_the_admin():
    provider = FakeVisionProvider()
    provider.vision_error = AIError("The model does not accept images.")
    service = make_service(provider)
    document = await service.extract("d.png", samples.png(samples.with_diagram()))
    assert document.pages[0].source == "ocr" and "Photosynthesis" in document.text
    assert "could not look at 1 page image" in document.warnings[0] and "OCR_VISION_MODEL" in document.warnings[0]
    service.shutdown()


async def test_unreadable_handwriting_is_read_by_the_vision_model_alone():
    """Local OCR finds nothing in scribbles; the vision model's reading is used and marked as unverified."""
    scribble = Image.new("RGB", (900, 500), "white")
    from PIL import ImageDraw
    draw = ImageDraw.Draw(scribble)
    for start in range(60, 800, 90):
        draw.arc((start, 150, start + 120, 330), 20, 320, fill="black", width=4)
    provider = FakeVisionProvider()
    provider.vision_replies = [json.dumps({"text": "1. B\n2. Osmosis is water moving through a membrane"})]
    service = make_service(provider)
    service._engines = [StubEngine("blind", "", 0.0)]
    document = await service.extract("answers.jpg", samples.jpeg(scribble))
    assert document.pages[0].source == "vision" and "Osmosis is water" in document.text
    assert document.pages[0].warnings == ["read by the AI model only"]

    provider.vision_replies = [json.dumps({"text": ""})]
    with pytest.raises(ExtractionError, match="No readable text"):
        await service.extract("answers.jpg", samples.jpeg(scribble))


async def test_vision_can_be_forced_and_switched_off():
    provider = FakeVisionProvider()
    provider.vision_replies = [json.dumps({"text": TEXT + "\n[Figure: none]"})]
    service = make_service(provider)
    document = await service.extract("p.png", samples.png(PAGE), force_vision=True)   # a clean page, but asked for
    assert document.pages[0].source == "vision" and len(provider.vision_calls) == 1

    off = make_service(provider, vision="off")
    await off.extract("d.png", samples.png(samples.with_diagram()), force_vision=True)
    assert len(provider.vision_calls) == 1
    service.shutdown(), off.shutdown()


async def test_vision_page_budget(ocr):
    provider = FakeVisionProvider()
    provider.vision_replies = [json.dumps({"text": " ".join(samples.PARAGRAPH[:2]) + "\n[Figure: shapes]"})]
    service = make_service(provider, vision_max_pages=1)
    document = await service.extract("d.pdf", samples.scanned_pdf([samples.with_diagram()] * 3))
    assert len(provider.vision_calls) == 1
    assert [p.source for p in document.pages].count("vision") == 1
    assert "2 more page(s) with formulas or figures were read as plain text only" in document.warnings[0]
    service.shutdown()


# ── the optional fallback engines, when they are installed ───────────────
@pytest.mark.skipif(not TesseractEngine.available(), reason="tesseract is not installed")
async def test_tesseract_engine_reads_and_uprights_pages():
    service = make_service(engines=("tesseract",))
    for image in (PAGE, PAGE.rotate(90, expand=True), tilted(PAGE, 7)):
        document = await service.extract("p.png", samples.png(image))
        assert samples.accuracy(TEXT, document.text) >= 0.97 and document.pages[0].engine.startswith("tesseract")
    service.shutdown()


@pytest.mark.skipif(not EasyOCREngine.available(), reason="easyocr is not installed")
async def test_easyocr_engine_reads_a_page():
    lines = EasyOCREngine().recognize(samples.bgr(PAGE))
    assert samples.accuracy(TEXT, " ".join(l.text for l in lines)) >= 0.95
    assert all(0 <= l.conf <= 1 and l.x1 > l.x0 for l in lines)
