"""OCR through the bot: photos, scanned PDFs and albums from admins and students, and /explain."""

import asyncio
import json

import pytest

pytest.importorskip("cv2")
pytest.importorskip("rapidocr")
pytest.importorskip("pypdfium2")

from PIL import Image  # noqa: E402

from bot.ai import AIError  # noqa: E402
from bot.handlers import reading  # noqa: E402
from tests import ocr_samples as samples  # noqa: E402
from tests.conftest import ADMIN_ID, STUDENT_ID, FakeVisionProvider  # noqa: E402

NOTES = samples.jpeg(samples.page())
ANSWERS = samples.jpeg(samples.page(["1. B", "2. Water moves across a membrane by osmosis"], size=(1100, 300)))


async def publish(h):
    await h.text(ADMIN_ID, "/start")
    await h.text(STUDENT_ID, "/start")
    await h.document(ADMIN_ID, "notes.txt", b"Mitochondria produce ATP. Osmosis moves water. " * 5)
    await h.press(ADMIN_ID, "pub:1")


def all_text(h, chat_id):
    return "\n".join(h.tg.texts(chat_id) + h.tg.texts(chat_id, "editMessageText"))


# ── admin: material from photos and scans ────────────────────────────────
async def test_admin_photo_of_notes_becomes_an_assessment(harness):
    h = harness
    await h.photo(ADMIN_ID, NOTES, caption="3 questions, easy")
    system, user = h.ai.calls[0]
    assert "Photosynthesis converts light energy" in user and "3 questions, easy" in user
    assessment = h.db.get_assessment(1)
    assert assessment["source_name"] == "photo" and "Osmosis moves water" in assessment["source_text"]
    assert h.tg.last_keyboard(ADMIN_ID) == ["pub:1", "regen:1", "del:1", "cmd:settings:1"]
    summary = all_text(h, ADMIN_ID)
    assert "Text read from the image" in summary and "OCR confidence 9" in summary  # 9x or 99/100 percent


async def test_admin_scanned_pdf_shows_progress_and_a_report(harness):
    h = harness
    typed = samples.text_pdf([["Enzymes are biological catalysts that speed up reactions."] * 6])
    pdf = samples.merge_pdfs(typed, samples.scanned_pdf([samples.page(), samples.two_columns()]))
    await h.document(ADMIN_ID, "chapter.pdf", pdf)
    material = h.db.get_assessment(1)["source_text"]
    assert "Enzymes are biological catalysts" in material        # the typed page, from its text layer
    assert "Calvin cycle" in material and "Capillaries link" in material  # the two scanned pages
    assert h.tg.texts(ADMIN_ID)[0].startswith("🔎 Reading the scanned pages")
    assert "3 pages: 1 with text, 2 scanned and read by OCR" in h.tg.texts(ADMIN_ID, "editMessageText")[-1]


async def test_admin_album_of_photos_is_one_assessment(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(reading, "ALBUM_WAIT", 0.3)
    first = samples.jpeg(samples.page(["The first page explains the cell membrane and what passes through it."]))
    second = samples.jpeg(samples.page(["The second page explains the nucleus and the DNA stored inside it."]))
    await asyncio.gather(h.photo(ADMIN_ID, first, caption="4 questions", album="g1"),
                         h.photo(ADMIN_ID, second, album="g1"))
    assert len(h.db.list_assessments()) == 1 and len(h.ai.calls) == 1
    assessment = h.db.get_assessment(1)
    assert assessment["source_name"] == "2 photos" and assessment["instructions"] == "4 questions"
    assert assessment["source_text"].index("cell membrane") < assessment["source_text"].index("nucleus")
    assert "Text read from 2 images" in all_text(h, ADMIN_ID)


async def test_generate_as_a_reply_to_a_photo(harness):
    h = harness
    h.ai.error = AIError("busy")   # the first attempt fails, so the photo is still without a draft
    photo = await h.photo(ADMIN_ID, NOTES)
    assert h.db.list_assessments() == []
    h.ai.error = None
    await h.reply(ADMIN_ID, "/generate 2 questions", to=photo)
    assert h.db.get_assessment(1)["instructions"] == "2 questions"


async def test_unreadable_upload_is_explained_and_nothing_is_created(harness):
    h = harness
    await h.photo(ADMIN_ID, samples.jpeg(Image.new("RGB", (900, 600), "white")))
    assert "No readable text was found" in h.tg.texts(ADMIN_ID)[-1]
    await h.document(ADMIN_ID, "broken.png", b"\x89PNG\r\n\x1a\n not an image")
    assert "could not be opened" in h.tg.texts(ADMIN_ID)[-1]
    assert h.ai.calls == [] and h.db.list_assessments() == []
    await h.document(ADMIN_ID, "notes.jpg", NOTES)  # an image sent as a file works like a photo
    assert "Photosynthesis" in h.db.get_assessment(1)["source_text"]


async def test_file_larger_than_the_limit_is_refused(make_harness):
    h = await make_harness(max_file_mb=0)
    await h.photo(ADMIN_ID, NOTES)
    assert "larger than 0 MB" in h.tg.texts(ADMIN_ID)[-1] and h.ai.calls == []


async def test_low_confidence_pages_are_reported_to_the_admin(harness):
    from tests.test_ocr_pipeline import GARBLED, StubEngine

    h = harness
    h.app.bot_data["services"].ocr._engines = [StubEngine("weak", GARBLED + ". " + GARBLED, 0.55)]
    await h.document(ADMIN_ID, "scan.pdf", samples.scanned_pdf([samples.page()] * 2))
    report = h.tg.texts(ADMIN_ID, "editMessageText")[-1]
    assert "⚠️ Low text confidence on page 1, 2" in report and "confidence 55%" in report
    assert h.db.get_assessment(1) is not None  # still usable: the admin decides


# ── students: handwritten answers, and things they want explained ────────
async def test_student_answers_with_a_photo(harness):
    h = harness
    await publish(h)
    await h.photo(STUDENT_ID, ANSWERS)
    shown = "\n".join(h.tg.texts(STUDENT_ID))
    assert "Text I found in your image" in shown and "osmosis" in shown   # they can check it before submitting
    assert h.tg.last_keyboard(STUDENT_ID) == ["ans:submit:1", "ans:undo:1", "ans:clear:0"]
    await h.press(STUDENT_ID, "ans:submit:1")
    submission = h.db.get_submission(1)
    assert submission["status"] == "graded" and "Water moves across a membrane" in submission["raw_text"]
    assert submission["raw_text"].replace(" ", "").startswith("1.B")


async def test_student_photo_with_nothing_to_answer_is_explained(harness):
    h = harness
    await h.text(STUDENT_ID, "/start")
    await h.photo(STUDENT_ID, NOTES)
    system, user = h.ai.calls[-1]
    assert system.startswith("You are a patient tutor") and "Photosynthesis converts light energy" in user
    assert h.tg.texts(STUDENT_ID)[-1] == "💡 It means water moves through a membrane &lt;slowly&gt;."
    assert h.db.list_assessments() == []  # a student's photo never becomes an assessment


async def test_explain_caption_wins_over_an_open_assessment(harness):
    h = harness
    await publish(h)
    await h.photo(STUDENT_ID, NOTES, caption="/explain why is oxygen released?")
    system, user = h.ai.calls[-1]
    assert system.startswith("You are a patient tutor")
    assert "<question>\nwhy is oxygen released?\n</question>" in user and "Water is split" in user
    assert h.tg.last_keyboard(STUDENT_ID) == []  # not stored as an answer
    assert h.tg.texts(STUDENT_ID)[-1].startswith("💡")


async def test_explain_command_variants(harness):
    h = harness
    await h.text(STUDENT_ID, "/explain what is osmosis?")
    assert "<material>\nwhat is osmosis?\n</material>" in h.ai.calls[-1][1]

    photo = await h.photo(ADMIN_ID, NOTES)  # (creates a draft; then the admin asks about the same photo)
    await h.reply(ADMIN_ID, "/explain the Calvin cycle part", to=photo)
    system, user = h.ai.calls[-1]
    assert system.startswith("You are a patient tutor") and "the Calvin cycle part" in user and "glucose" in user

    await h.text(STUDENT_ID, "/explain")
    assert "Send a photo or file" in h.tg.texts(STUDENT_ID)[-1]

    before = len(h.ai.calls)
    h.ai.replies = ["not json", "still not json"]
    await h.text(STUDENT_ID, "/explain what is ATP?")
    assert len(h.ai.calls) == before + 2 and "couldn\'t explain that just now" in h.tg.texts(STUDENT_ID)[-1]


async def test_explain_can_be_switched_off(make_harness):
    h = await make_harness(allow_explain=False)
    await h.text(STUDENT_ID, "/explain what is osmosis?")
    assert "Explanations aren\'t available right now" in h.tg.texts(STUDENT_ID)[-1] and h.ai.calls == []
    await h.photo(STUDENT_ID, NOTES)  # nothing open and no explanations: the usual hint
    assert "No assessments are waiting" in h.tg.texts(STUDENT_ID)[-1] and h.ai.calls == []


async def test_students_have_a_smaller_page_budget(make_harness):
    from bot.ocr import OCRConfig

    h = await make_harness(ocr=OCRConfig(max_pages_user=1, engines=("rapidocr",)))
    await publish(h)
    pages = [samples.page([f"{n}. Answer number {n} is here."], size=(900, 200)) for n in (1, 2)]
    await h.document(STUDENT_ID, "answers.pdf", samples.scanned_pdf(pages))
    assert "only the first 1 were read" in all_text(h, STUDENT_ID)
    await h.press(STUDENT_ID, "ans:submit:1")
    raw = h.db.get_submission(1)["raw_text"]
    assert "Answer number 1" in raw and "Answer number 2" not in raw


async def test_vision_model_is_used_for_explanations_when_the_provider_can_see(harness):
    h = harness
    provider = FakeVisionProvider()
    provider.vision_replies = [json.dumps({
        "text": " ".join(samples.PARAGRAPH[:2]) + "\n[Figure: a cell with a labelled chloroplast]"})]
    h.app.bot_data["services"].ai.provider = provider
    await h.photo(STUDENT_ID, samples.jpeg(samples.with_diagram()), caption="/explain")
    assert len(provider.vision_calls) == 1
    system, user = provider.calls[-1]
    assert "[Figure: a cell with a labelled chloroplast]" in user  # the tutor gets the figure description
    assert "read by the AI vision model" in all_text(h, STUDENT_ID)


async def test_a_second_upload_while_one_is_being_read_is_turned_away(harness):
    h = harness
    await h.text(ADMIN_ID, "/start")
    big = samples.scanned_pdf([samples.page()] * 3)
    await asyncio.gather(h.document(ADMIN_ID, "one.pdf", big), h.document(ADMIN_ID, "two.pdf", big))
    assert any("still reading your previous file" in t for t in h.tg.texts(ADMIN_ID))
    assert len(h.db.list_assessments()) == 1
