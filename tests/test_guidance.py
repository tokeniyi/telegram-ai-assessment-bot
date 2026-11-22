"""Stage 6: command guidance. The registry against the real handlers, typo suggestions, argument
checks with real ids, permissions, next steps, buttons, and grading retries.

Everything is driven through the bot with the fake Telegram API and AI provider.
"""

import asyncio
import json
import logging
import re
import time

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from bot import commands, guidance
from bot.ai import AIError, AIUnavailable
from bot.commands import COMMANDS, Suggestion
from bot.handlers import guide
from tests.conftest import ADMIN_ID, STUDENT_ID, Crash
from tests.test_flow import MATERIAL, publish
from tests.test_recovery import STUDENTS, draft_with_students, expire_grading_leases

ADMIN_ONLY = sorted(c.name for c in COMMANDS.values() if c.role == commands.ADMIN)
ADMIN_COMMAND = re.compile(r"/(%s)\b" % "|".join(ADMIN_ONLY))


def sent(h, chat, method="sendMessage"):
    """(text, button callback data) of every message of `method` to `chat`."""
    out = []
    for m, p in h.tg.calls:
        if m == method and int(p.get("chat_id", 0)) == chat:
            markup = p.get("reply_markup")
            markup = json.loads(markup) if isinstance(markup, str) else markup
            buttons = [b["callback_data"] for row in (markup or {}).get("inline_keyboard", []) for b in row]
            out.append((p.get("text", ""), buttons))
    return out


def last(h, chat, method="sendMessage"):
    return sent(h, chat, method)[-1]


async def command(h, user_id, text):
    """Send a command; return (text, buttons) of the bot's answer (a message or an edit)."""
    before = len(h.tg.calls)
    await h.text(user_id, text)
    answers = [(m, p) for m, p in h.tg.calls[before:] if m in ("sendMessage", "editMessageText")
               and int(p.get("chat_id", 0)) == user_id]
    assert answers, f"no answer to {text}"
    method = answers[-1][0]
    return last(h, user_id, method)


def callbacks_of(application):
    return [h for handlers in application.handlers.values() for h in handlers]


# ── the registry is what the bot really handles ──────────────────────────
async def test_the_registry_lists_exactly_the_registered_commands(harness):
    handlers = callbacks_of(harness.app)
    registered = {name for h in handlers if isinstance(h, CommandHandler) for name in h.commands}
    names = {c.name for c in COMMANDS.values()} | {a for c in COMMANDS.values() for a in c.aliases}
    assert registered == names
    assert all(not c.aliases for c in COMMANDS.values())  # none today; get() and closest() support them


async def test_every_command_answers_with_its_role_enforced(harness):
    """Each command, typed by a student and by an admin, reaches its own handler (not the
    unknown-command reply), and admin-only commands refuse students."""
    h = harness
    await publish(h)
    for name, spec in COMMANDS.items():
        for user_id in (STUDENT_ID, ADMIN_ID):
            text, _ = await command(h, user_id, f"/{name}")
            assert "I don't know" not in text, name
            refused = "administrators only" in text
            assert refused == (spec.role == commands.ADMIN and user_id == STUDENT_ID), (name, user_id, text)
            if refused:
                assert "/help" in text and not ADMIN_COMMAND.search(text)


async def test_commands_answer_only_in_private_chats(harness):
    """The registry says commands are for private chats: in a group nothing answers."""
    h = harness
    for name in COMMANDS:
        before = len(h.tg.calls)
        await h.deliver({"update_id": 10_000 + len(h.tg.calls), "message": {
            "message_id": 1, "date": int(time.time()), "chat": {"id": -100, "type": "group", "title": "g"},
            "from": {"id": ADMIN_ID, "is_bot": False, "first_name": "A"}, "text": f"/{name}",
            "entities": [{"type": "bot_command", "offset": 0, "length": len(name) + 1}]}})
        assert [m for m, _ in h.tg.calls[before:] if m.startswith(("send", "edit"))] == [], name


async def test_feature_switches_hide_a_command_from_those_who_cannot_use_it(make_harness):
    h = await make_harness(allow_explain=False)
    settings = h.app.bot_data["services"].settings
    assert "explain" not in {c.name for c in commands.available(settings, STUDENT_ID)}
    assert "explain" in {c.name for c in commands.available(settings, ADMIN_ID)}
    assert "explain" not in {c.command for c in commands.menu(admin=False, settings=settings)}
    text, _ = await command(h, STUDENT_ID, "/help")
    assert "/explain" not in text.split("Useful commands")[1]
    text, _ = await command(h, STUDENT_ID, "/explain what is osmosis?")
    assert "aren't available" in text  # the handler itself still refuses


async def test_menus_and_help_come_from_the_registry(harness):
    h = harness
    settings = h.app.bot_data["services"].settings
    user_menu = [c.command for c in commands.menu(admin=False, settings=settings)]
    admin_menu = [c.command for c in commands.menu(admin=True, settings=settings)]
    assert not set(user_menu) & set(ADMIN_ONLY) and set(user_menu) < set(admin_menu)
    assert "myid" not in admin_menu  # menu=False, as before Stage 6

    student_help, _ = await command(h, STUDENT_ID, "/help")
    admin_help, _ = await command(h, ADMIN_ID, "/help")
    for spec in commands.available(settings, STUDENT_ID):
        if spec.name != "start":
            assert spec.usage.replace("<", "&lt;").replace(">", "&gt;") in student_help, spec.name
    assert not ADMIN_COMMAND.search(student_help)
    for name in ADMIN_ONLY:
        assert f"/{name}" in admin_help


async def test_every_button_command_is_routed_to_its_real_handler(harness):
    h = harness
    pattern = next(x.pattern for x in callbacks_of(h.app)
                   if isinstance(x, CallbackQueryHandler) and x.callback is guide.on_command_button)
    mapped = h.app.bot_data["command_callbacks"]
    for spec in COMMANDS.values():
        if spec.button:
            args = ("1",) if any(a.required for a in spec.args) else ()
            data = Suggestion(spec.name, args).callback_data
            assert pattern.match(data) and mapped[spec.name] is not guide.on_command_button, spec.name
    # the unknown-command reply is the last handler, so it never shadows a real command
    assert isinstance(callbacks_of(h.app)[-1], MessageHandler)


def test_suggestions_are_checked_against_the_registry():
    for bad in (lambda: Suggestion("nope"), lambda: Suggestion("close"),
                lambda: Suggestion("close", ("abc",)), lambda: Suggestion("close", ("1", "2")),
                lambda: Suggestion("close", ("1",), button="Close")):  # /close is not safe on a button
        with pytest.raises(ValueError):
            bad()
    assert Suggestion("regrade", ("#3",), button="x").callback_data == "cmd:regrade:3"
    assert Suggestion("results", ("3",), why="Scores").html() == "Scores: <code>/results 3</code>"
    with pytest.raises(ValueError):
        _ = Suggestion("explain", ("why?",)).callback_data


# ── unknown and mistyped commands ────────────────────────────────────────
async def test_a_mistyped_command_suggests_the_closest_one_the_user_may_use(harness):
    h = harness
    await publish(h)
    text, _ = await command(h, STUDENT_ID, "/pendng")
    assert "I don't know /pendng. Did you mean" in text and "/pending" in text

    text, _ = await command(h, STUDENT_ID, "/regrad 1")  # close to an admin command
    assert not ADMIN_COMMAND.search(text) and "/help" in text

    text, _ = await command(h, STUDENT_ID, "/xyzzy")
    assert text.startswith("✏️ I don't know /xyzzy.") and "/help" in text

    text, _ = await command(h, ADMIN_ID, "/statsu")
    assert "/status" in text
    text, _ = await command(h, ADMIN_ID, "/regrad")  # nothing failed: no invented id
    assert "/regrade &lt;id&gt;" in text and "nothing needs it right now" in text and "<code>" not in text


# ── arguments are checked, and examples use real ids ─────────────────────
async def test_assessment_arguments_are_checked_and_examples_use_real_ids(harness):
    h = harness
    await publish(h)                                     # #1 is open
    await h.document(ADMIN_ID, "more.txt", MATERIAL)     # #2 is a draft

    text, _ = await command(h, ADMIN_ID, "/close")
    assert text.startswith("✏️ /close needs an assessment number") and "<code>/close 1</code>" in text
    assert "/close 2" not in text  # a draft cannot be closed, so it is not offered
    text, _ = await command(h, ADMIN_ID, "/close abc")
    assert "“abc” is not an assessment number" in text and "<code>/close 1</code>" in text
    text, _ = await command(h, ADMIN_ID, "/close 99")
    assert "There is no assessment #99" in text and "<code>/close 1</code>" in text
    text, _ = await command(h, ADMIN_ID, "/close 2")
    assert text.startswith("ℹ️ Assessment #2 is still a draft") and "Send to all users" in text
    text, _ = await command(h, ADMIN_ID, "/results 2")
    assert text.startswith("ℹ️ Assessment #2 is still a draft")

    text, buttons = await command(h, ADMIN_ID, "/close 1")
    assert "is closed" in text and "<code>/results 1</code>" in text and buttons == ["cmd:results:1"]
    text, buttons = await command(h, ADMIN_ID, "/close 1")
    assert text.startswith("ℹ️ Assessment #1 is already closed") and buttons == ["cmd:results:1"]
    text, _ = await command(h, ADMIN_ID, "/close")
    assert "No assessment is open, so there is nothing to close" in text and "<code>" not in text
    text, _ = await command(h, ADMIN_ID, "/regrade")
    assert "No grading has failed, so there is nothing to retry" in text and "<code>" not in text


async def test_generate_explains_how_it_is_used(harness):
    h = harness
    text, _ = await command(h, ADMIN_ID, "/generate")
    assert text.startswith("✏️ /generate works as a reply") and "<code>/generate 8 questions, easy</code>" in text


# ── permissions ──────────────────────────────────────────────────────────
async def test_students_are_never_shown_admin_commands(make_harness):
    h = await make_harness(allow_chat=False)
    await draft_with_students(h, STUDENTS[:2])
    await h.press(ADMIN_ID, "pub:1")
    await h.document(ADMIN_ID, "more.txt", MATERIAL)
    await h.press(ADMIN_ID, "pub:2")
    student = STUDENTS[0]
    h.ai.error = AIError("bad reply")
    await h.text(student, "1. B")
    await h.press(student, "ans:submit:2")              # grading fails
    h.ai.error = None
    for typed in ("/myresults", "/pending", "/statsu", "/regrade 2", "/results", "/help", "/cancel"):
        await h.text(student, typed)
    await h.text(ADMIN_ID, "/close 1")
    await h.text(student, "1. B")
    await h.press(student, "ans:submit:1")               # closed meanwhile
    texts = [t for t, _ in sent(h, student) + sent(h, student, "editMessageText")]
    assert texts and not [t for t in texts if ADMIN_COMMAND.search(t)]
    assert not [b for _, buttons in sent(h, student) for b in buttons if b.split(":")[1] in ADMIN_ONLY]


def test_render_drops_a_suggestion_the_user_may_not_run(caplog):
    from tests.conftest import make_settings
    settings = make_settings("postgresql://x/y")
    with caplog.at_level(logging.WARNING):
        text, markup = guidance.render(settings, STUDENT_ID, "x", [Suggestion("regrade", ("1",), button="Retry")])
    assert text == "x" and markup is None and "Not suggesting /regrade 1" in caplog.text


async def test_buttons_check_permission_and_only_run_safe_commands(harness):
    h = harness
    await publish(h)
    await h.text(STUDENT_ID, "1. B")
    h.ai.error = AIError("bad reply")
    await h.press(STUDENT_ID, "ans:submit:1")
    h.ai.error = None
    calls = len(h.ai.calls)

    await h.press(STUDENT_ID, "cmd:regrade:1")           # a forged button: refused
    answer = [p for m, p in h.tg.calls if m == "answerCallbackQuery"][-1]
    assert "administrators only" in answer["text"] and len(h.ai.calls) == calls
    assert h.db.get_submission(1)["status"] == "failed"

    await h.press(ADMIN_ID, "cmd:close:1")               # /close is not allowed on a button
    answer = [p for m, p in h.tg.calls if m == "answerCallbackQuery"][-1]
    assert "no longer works" in answer["text"] and h.db.get_assessment(1)["status"] == "published"
    await h.press(ADMIN_ID, "cmd:nosuch")
    assert "no longer works" in [p for m, p in h.tg.calls if m == "answerCallbackQuery"][-1]["text"]

    await h.press(ADMIN_ID, "cmd:regrade:1")             # the admin's button runs /regrade 1
    assert h.db.get_submission(1)["status"] == "graded"
    await h.press(ADMIN_ID, "cmd:results:1")
    assert last(h, ADMIN_ID)[0].startswith("📊 <b>#1")


# ── next steps, only when there is one ───────────────────────────────────
async def test_next_steps_are_suggested_only_when_there_is_one(make_harness):
    h = await make_harness(allow_chat=False)
    await draft_with_students(h, [STUDENT_ID])
    await h.press(ADMIN_ID, "pub:1")
    report, buttons = last(h, ADMIN_ID)
    assert "<code>/results 1</code>" in report and buttons == ["cmd:results:1"]

    text, _ = await command(h, STUDENT_ID, "/myresults")
    assert "1 assessment still to answer: /pending" in text
    await h.document(ADMIN_ID, "more.txt", MATERIAL)
    await h.press(ADMIN_ID, "pub:2")
    await h.text(STUDENT_ID, "1. B")                     # answers the newest, #2
    await h.press(STUDENT_ID, "ans:submit:2")
    result, buttons = last(h, STUDENT_ID)
    assert "Result" in result and "1 assessment still to answer: /pending" in result and buttons == ["cmd:pending"]

    await h.press(STUDENT_ID, "show:1")
    await h.text(STUDENT_ID, "1. B")
    await h.press(STUDENT_ID, "ans:submit:1")
    result, buttons = last(h, STUDENT_ID)
    assert "Result" in result and "/pending" not in result and buttons == []  # nothing left to do

    text, _ = await command(h, STUDENT_ID, "/pending")
    assert "all caught up" in text and "Your scores so far: /myresults" in text
    await h.text(ADMIN_ID, "/close 1")
    await h.text(ADMIN_ID, "/close 2")
    text, _ = await command(h, STUDENTS[0], "/pending")  # nothing open, and nothing submitted
    assert "all caught up" in text and "/myresults" not in text


async def test_publishing_to_nobody_suggests_nothing_to_follow(harness, monkeypatch):
    h = harness
    await h.document(ADMIN_ID, "notes.txt", MATERIAL)
    publish_for_real = h.db.publish_assessment

    def publish_to_nobody(assessment_id, published_by):  # the press itself registers the admin
        publish_for_real(assessment_id, published_by)
        h.db._run("DELETE FROM deliveries")
        return 0

    monkeypatch.setattr(h.db, "publish_assessment", publish_to_nobody)
    await h.press(ADMIN_ID, "pub:1")
    report, buttons = last(h, ADMIN_ID)
    assert "sent to 0 users" in report and "/results" not in report and buttons == []


async def test_myresults_tells_failed_from_in_progress_grading(make_harness):
    h = await make_harness(allow_chat=False, allow_resubmission=True)
    await publish(h)
    h.ai.error = AIError("bad reply")
    await h.text(STUDENT_ID, "1. B")
    await h.press(STUDENT_ID, "ans:submit:1")
    h.ai.error = Crash()
    await h.text(STUDENT_ID, "1. A")
    with pytest.raises(Crash):
        await h.press(STUDENT_ID, "ans:submit:1")
    h.ai.error = None
    text, _ = await command(h, STUDENT_ID, "/myresults")
    assert "being graded; the result will be sent to you" in text
    assert "grading delayed; your answers are saved" in text and not ADMIN_COMMAND.search(text)


# ── grading: suggest /regrade only when a manual retry is what is needed ──
async def test_results_suggest_regrade_only_for_failed_grading(make_harness):
    h = await make_harness(allow_chat=False)
    await draft_with_students(h, STUDENTS[:3])
    await h.press(ADMIN_ID, "pub:1")
    failing, crashing, interrupted = STUDENTS[:3]
    for user_id, error in ((crashing, Crash()), (interrupted, Crash())):
        await h.text(user_id, "1. B")
        h.ai.error = error
        with pytest.raises(Crash):
            await h.press(user_id, "ans:submit:1")
    expire_grading_leases(h.db)                          # the second one's lease runs out ...
    h.db._run("UPDATE submissions SET grading_claimed_until = now() + interval '5 minutes' WHERE id = 1")
    h.ai.error = None

    text, buttons = await command(h, ADMIN_ID, "/results 1")
    assert "⏳ 1 being graded now. No action needed." in text
    assert "🔁 1 interrupted; graded again automatically. No action needed." in text
    assert "/regrade" not in text and buttons == []      # nothing for the admin to do

    await h.text(failing, "1. B")
    h.ai.error = AIError("bad reply")
    await h.press(failing, "ans:submit:1")
    h.ai.error = None
    text, buttons = await command(h, ADMIN_ID, "/results 1")
    assert "❌ 1 submission could not be graded." in text and "<code>/regrade 1</code>" in text
    assert buttons == ["cmd:regrade:1"]


async def test_regrade_reports_what_needs_no_action(make_harness):
    h = await make_harness(allow_chat=False)
    await publish(h)
    text, _ = await command(h, ADMIN_ID, "/regrade 1")
    assert "Nobody has submitted answers to #1 yet" in text
    await h.text(STUDENT_ID, "1. B")
    await h.press(STUDENT_ID, "ans:submit:1")
    text, buttons = await command(h, ADMIN_ID, "/regrade 1")
    assert "Every submission to #1 is already graded" in text and buttons == ["cmd:results:1"]


async def test_a_double_tap_on_retry_grades_each_submission_once(make_harness):
    h = await make_harness(allow_chat=False)
    await draft_with_students(h, STUDENTS[:3])
    await h.press(ADMIN_ID, "pub:1")
    h.ai.error = AIError("bad reply")
    for user_id in STUDENTS[:3]:
        await h.text(user_id, "1. B")
        await h.press(user_id, "ans:submit:1")
    h.ai.error = None
    retry = [b for _, buttons in sent(h, ADMIN_ID) for b in buttons if b.startswith("cmd:regrade")]
    assert retry and retry[0] == "cmd:regrade:1"         # the failure notice carries the button

    complete, graded = h.ai.complete, []

    async def slow(system, user):
        if "<submission>" in user:                       # a grading request
            graded.append(user)
            await asyncio.sleep(0.05)                    # long enough for the taps to overlap
        return await complete(system, user)

    h.ai.complete = slow
    other = await make_harness(allow_chat=False)         # and another process's admin
    other_complete = other.ai.complete

    async def other_slow(system, user):
        if "<submission>" in user:
            graded.append(user)
            await asyncio.sleep(0.05)
        return await other_complete(system, user)

    other.ai.complete = other_slow
    await asyncio.gather(h.press(ADMIN_ID, "cmd:regrade:1"), h.press(ADMIN_ID, "cmd:regrade:1"),
                         other.text(ADMIN_ID, "/regrade 1"))
    assert len(graded) == 3                              # each submission graded once
    for user_id in STUDENTS[:3]:
        results = [t for t, _ in sent(h, user_id) + sent(other, user_id) if "Result" in t]
        assert len(results) == 1, user_id
    reports = [t for t, _ in sent(h, ADMIN_ID) + sent(other, ADMIN_ID) if "being graded already" in t
               or "being graded right now" in t]
    assert reports and all("No action needed" in t for t in reports)


@pytest.mark.parametrize("error, kind", [
    (AIUnavailable("Could not reach the AI provider."), "⏳"),
    (AIError("The AI returned a reply that could not be understood."), "ℹ️"),
])
async def test_a_regrade_stopped_by_the_ai_says_why_and_offers_a_retry(make_harness, error, kind):
    h = await make_harness(allow_chat=False)
    await publish(h)
    await h.text(STUDENT_ID, "1. B")
    h.ai.error = AIError("bad reply")
    await h.press(STUDENT_ID, "ans:submit:1")
    h.ai.error = error
    text, buttons = await command(h, ADMIN_ID, "/regrade 1")
    assert text.startswith(f"{kind} Stopped after grading 0.") and "1 submission still to grade" in text
    assert "<code>/regrade 1</code>" in text and buttons == ["cmd:regrade:1"]


async def test_generation_failures_tell_an_outage_from_a_bad_reply(harness):
    h = harness
    h.ai.error = AIUnavailable("Could not reach the AI provider.")
    await h.document(ADMIN_ID, "notes.txt", MATERIAL)
    text, buttons = last(h, ADMIN_ID, "editMessageText")
    assert text.startswith("❌ ⏳ Could not generate questions. The AI provider is not answering right now")
    assert "Nothing was saved; send the material again in a few minutes" in text and "/status" in text
    h.ai.error = AIError("The AI response was cut off because it was too long. Ask for fewer questions.")
    await h.document(ADMIN_ID, "notes.txt", MATERIAL)
    text, _ = last(h, ADMIN_ID, "editMessageText")
    assert "Ask for fewer questions" in text and "/status" not in text


async def test_the_ai_down_alert_does_not_suggest_a_retry_that_cannot_work(harness):
    h = harness
    await h.text(ADMIN_ID, "/start")
    h.ai.error = AIUnavailable("Could not reach the AI provider.")
    for _ in range(3):
        await h.text(STUDENT_ID, "hello")
    alert, buttons = [m for m in sent(h, ADMIN_ID) if m[0].startswith("🚨")][0]
    assert "/regrade" not in alert and buttons == ["cmd:status"]


async def test_the_chat_model_is_told_only_the_commands_the_user_may_use(harness):
    h = harness
    await h.text(STUDENT_ID, "hello")
    await h.text(ADMIN_ID, "hello")
    student_context, admin_context = [u for s, u in h.ai.calls if s.startswith("You are the chat assistant")]
    assert "Commands this user can use: /start" in student_context and not ADMIN_COMMAND.search(student_context)
    assert "/regrade <id>" in admin_context


async def test_a_command_button_does_nothing_outside_a_private_chat(harness):
    h = harness
    await publish(h)
    await h.text(STUDENT_ID, "1. B")
    h.ai.error = AIError("bad reply")
    await h.press(STUDENT_ID, "ans:submit:1")
    h.ai.error = None
    calls = len(h.ai.calls)
    await h.deliver({"update_id": 90_000, "callback_query": {
        "id": "cb", "from": {"id": ADMIN_ID, "is_bot": False, "first_name": "A"}, "chat_instance": "ci",
        "data": "cmd:regrade:1", "message": {"message_id": 1, "date": int(time.time()), "text": "x",
                                             "chat": {"id": -100, "type": "group", "title": "g"}}}})
    assert len(h.ai.calls) == calls and h.db.get_submission(1)["status"] == "failed"


async def test_with_resubmission_the_graded_assessment_is_not_offered_as_still_to_answer(make_harness):
    h = await make_harness(allow_chat=False, allow_resubmission=True)
    await publish(h)
    await h.text(STUDENT_ID, "1. B")
    await h.press(STUDENT_ID, "ans:submit:1")
    result, buttons = last(h, STUDENT_ID)
    assert "Result" in result and "still to answer" not in result and buttons == []


async def test_reply_and_caption_invocations_in_the_registry_really_work(harness, monkeypatch):
    """The bot has no conversation states; commands that work as a reply or as a caption are the
    equivalent, and the registry's `invocations` must match how they are really routed."""
    from bot.handlers import explain
    h = harness
    as_caption = {c.name for c in COMMANDS.values() if "caption" in c.invocations}
    assert as_caption == {n for n in COMMANDS if explain.caption_question(f"/{n} x") is not None}

    routed = []

    async def record(messages, context, question):
        routed.append(question)

    monkeypatch.setattr(explain, "explain_upload", record)
    await h.document(STUDENT_ID, "notes.txt", MATERIAL, caption="/explain why?")
    assert routed == ["why?"]

    notes = {"message_id": 77, "date": int(time.time()), "chat": {"id": ADMIN_ID, "type": "private"},
             "from": {"id": ADMIN_ID, "is_bot": False, "first_name": "A"}, "text": MATERIAL.decode()}
    for name in sorted(c.name for c in COMMANDS.values() if "reply" in c.invocations):
        calls = len(h.ai.calls)
        await h.reply(ADMIN_ID, f"/{name}", to=notes)
        assert len(h.ai.calls) == calls + 1, name      # the replied-to text was used as its input
    assert h.db.get_assessment(1)["source_name"] == "pasted text"  # /generate made a draft from it
