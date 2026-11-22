"""End-to-end: admin uploads material -> users answer -> answers are graded and stored."""

import json

from bot.ai import AIError
from tests.conftest import ADMIN_ID, STUDENT_ID

MATERIAL = ("Mitochondria produce most of the cell's ATP. Osmosis is the movement of water "
            "across a semi-permeable membrane. " * 5).encode()


async def publish(h):
    await h.text(ADMIN_ID, "/start")
    await h.text(STUDENT_ID, "/start")
    await h.document(ADMIN_ID, "notes.txt", MATERIAL, caption="2 questions")
    assert h.tg.last_keyboard(ADMIN_ID) == ["pub:1", "regen:1", "del:1", "cmd:settings:1"]
    await h.press(ADMIN_ID, "pub:1")


async def test_full_flow(harness):
    h = harness
    await publish(h)

    # the admin instructions and the material reached the AI
    system, user = h.ai.calls[0]
    assert "2 questions" in user and "Mitochondria produce" in user

    # the draft preview shows reference answers to the admin only
    admin_text = "\n".join(h.tg.texts(ADMIN_ID))
    assert "Draft #1" in admin_text and "Answer: B) Mitochondria" in admin_text
    student_text = "\n".join(h.tg.texts(STUDENT_ID))
    assert "Which organelle" in student_text and "B) Mitochondria" in student_text  # option, de-duplicated letter
    assert "Answer:" not in student_text and "semi-permeable" not in student_text
    assert "sent to 2 users" in admin_text

    # the student answers in two messages, then submits
    await h.text(STUDENT_ID, "1. B")
    await h.text(STUDENT_ID, "2) water moving")
    assert h.tg.last_keyboard(STUDENT_ID) == ["ans:submit:1", "ans:undo:1", "ans:clear:0"]
    await h.press(STUDENT_ID, "ans:submit:1")

    system, user = h.ai.calls[-1]
    assert "1. B\n2) water moving" in user and "Reference answer" in user

    submission = h.db.get_submission(1)
    assert submission["status"] == "graded"
    assert submission["raw_text"] == "1. B\n2) water moving"
    assert submission["score"] == 4 and submission["max_score"] == 4  # 99 clamped to the 3-point maximum
    assert json.loads(submission["details"])[1]["awarded"] == 3

    result = h.tg.texts(STUDENT_ID)[-1]
    assert "4 / 4" in result and "100%" in result
    assert "&lt;keep going&gt;" in result  # AI text is HTML-escaped

    # a second submission is not possible
    await h.text(STUDENT_ID, "1. A")  # nothing is open any more, so this is only chat
    assert "No assessment is open" in h.ai.calls[-1][1] and h.db.get_submission(2) is None

    # admin results: summary plus CSV
    await h.text(ADMIN_ID, "/results")
    assert "1 submitted" in "\n".join(h.tg.texts(ADMIN_ID)[-2:])
    assert any(m == "sendDocument" for m, _ in h.tg.calls)

    await h.text(STUDENT_ID, "/myresults")
    assert "4/4" in h.tg.texts(STUDENT_ID)[-1]


async def test_non_admin_cannot_create_or_publish(harness):
    h = harness
    await h.text(STUDENT_ID, "/start")
    await h.document(STUDENT_ID, "notes.txt", MATERIAL)
    assert h.ai.calls == [] and h.db.list_assessments() == []
    await h.text(STUDENT_ID, "/results")
    assert "administrators only" in h.tg.texts(STUDENT_ID)[-1]

    await h.document(ADMIN_ID, "notes.txt", MATERIAL)
    await h.press(STUDENT_ID, "pub:1")
    assert h.db.get_assessment(1)["status"] == "draft"


async def test_student_can_answer_with_a_file(harness):
    h = harness
    await publish(h)
    await h.document(STUDENT_ID, "answers.txt", b"1. B\n2. water moves through a membrane by osmosis")
    await h.press(STUDENT_ID, "ans:submit:1")
    assert "water moves through a membrane" in h.db.get_submission(1)["raw_text"]
    assert h.db.get_submission(1)["status"] == "graded"


async def test_failed_grading_is_kept_and_regraded(harness):
    h = harness
    await publish(h)
    await h.text(STUDENT_ID, "1. B 2. water")
    h.ai.error = AIError("provider down")
    await h.press(STUDENT_ID, "ans:submit:1")
    assert h.db.get_submission(1)["status"] == "failed"
    assert "saved" in h.tg.texts(STUDENT_ID)[-1]
    assert "/regrade 1" in h.tg.texts(ADMIN_ID)[-1]

    h.ai.error = None
    await h.text(ADMIN_ID, "/regrade 1")
    assert h.db.get_submission(1)["status"] == "graded"
    assert "Result" in h.tg.texts(STUDENT_ID)[-1]


async def test_generation_error_is_reported_and_nothing_saved(harness):
    h = harness
    h.ai.error = AIError("The AI provider rejected the API key. Check AI_API_KEY in .env.")
    await h.document(ADMIN_ID, "notes.txt", MATERIAL)
    assert "rejected the API key" in h.tg.texts(ADMIN_ID, "editMessageText")[-1]
    assert h.db.list_assessments() == []


async def test_malformed_ai_reply_is_retried(harness):
    h = harness
    h.ai.replies = ["Sure! Here you go: not json"]
    await h.document(ADMIN_ID, "notes.txt", MATERIAL)
    assert len(h.ai.calls) == 2 and h.db.get_assessment(1)["title"] == "Cell Biology Basics"


async def test_regenerate_discard_close_and_pending(harness):
    h = harness
    await h.text(STUDENT_ID, "/start")
    await h.document(ADMIN_ID, "notes.txt", MATERIAL)
    await h.press(ADMIN_ID, "regen:1")
    assert len(h.ai.calls) == 2 and len(h.db.get_questions(1)) == 2
    await h.press(ADMIN_ID, "del:1")
    assert h.db.get_assessment(1) is None

    await h.text(ADMIN_ID, "Some pasted notes")  # no open assessment: it is chat, and is not stored
    assert "Chat reply to: Some pasted notes" in h.tg.texts(ADMIN_ID)[-1]
    assert "sending a PDF, DOCX or TXT" in h.ai.calls[-1][1]  # the AI is told how admins create assessments

    await h.document(ADMIN_ID, "notes.txt", MATERIAL)
    await h.press(ADMIN_ID, "pub:2")
    await h.press(ADMIN_ID, "pub:2")  # double tap does not send twice
    assert sum("Which organelle" in t for t in h.tg.texts(STUDENT_ID)) == 1

    await h.text(STUDENT_ID, "/pending")
    assert h.tg.last_keyboard(STUDENT_ID) == ["show:2"]
    await h.press(STUDENT_ID, "show:2")
    assert sum("Which organelle" in t for t in h.tg.texts(STUDENT_ID)) == 2

    await h.text(STUDENT_ID, "1. B")
    await h.text(ADMIN_ID, "/close 2")
    await h.press(STUDENT_ID, "ans:submit:2")
    assert h.db.latest_submissions(2) == []
    assert "closed" in h.tg.texts(STUDENT_ID, "editMessageText")[-1]


async def test_long_material_is_cut_and_admin_is_told(make_harness):
    h = await make_harness(max_document_chars=300)
    await h.document(ADMIN_ID, "notes.txt", MATERIAL)
    assert "only the first 300" in h.tg.texts(ADMIN_ID)[0]
    assert len(h.db.get_assessment(1)["source_text"]) == 300


async def test_resubmission_when_enabled(make_harness):
    h = await make_harness(allow_resubmission=True)
    await publish(h)
    for _ in range(2):
        await h.text(STUDENT_ID, "1. B")
        await h.press(STUDENT_ID, "ans:submit:1")
    assert len(h.db.latest_submissions(1)) == 1 and h.db.get_submission(2)["status"] == "graded"
