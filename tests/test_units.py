import io

import pytest

from bot.ai.service import normalize_grades, normalize_questions, parse_json_object
from bot.config import ConfigError, load_settings
from bot.extract import ExtractionError, extract_text
from bot.utils import pack_blocks

ENV_KEYS = ["TELEGRAM_BOT_TOKEN", "ADMIN_IDS", "AI_PROVIDER", "AI_API_KEY", "AI_MODEL", "AI_BASE_URL",
            "DEFAULT_NUM_QUESTIONS", "MAX_QUESTIONS", "SHOW_CORRECT_ANSWERS", "DATABASE_PATH"]


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    def write(text):
        path = tmp_path / ".env"
        path.write_text(text)
        return path

    yield write
    # load_dotenv writes into os.environ; monkeypatch.delenv above restores the originals afterwards
    import os
    for key in ENV_KEYS:
        os.environ.pop(key, None)


def test_settings_from_env_file(env_file):
    s = load_settings(env_file("TELEGRAM_BOT_TOKEN=1:abc\nADMIN_IDS=5, 6\nAI_PROVIDER=gemini\nAI_API_KEY=k\n"))
    assert s.admin_ids == {5, 6} and s.is_admin(5) and not s.is_admin(7)
    assert s.ai_model == "gemini-2.5-flash" and "generativelanguage" in s.ai_base_url


def test_settings_default_to_kilo_free_auto_model(env_file):
    s = load_settings(env_file("TELEGRAM_BOT_TOKEN=1:abc\nAI_API_KEY=k\n"))
    assert (s.ai_provider, s.ai_model) == ("kilo", "kilo-auto/free")
    assert s.ai_base_url == "https://api.kilo.ai/api/gateway"


def test_settings_anthropic(env_file):
    s = load_settings(env_file("TELEGRAM_BOT_TOKEN=1:abc\nAI_API_KEY=k\nAI_PROVIDER=anthropic\n"))
    assert (s.ai_model, s.ai_base_url) == ("claude-opus-5-5", None)


def test_settings_report_every_problem(env_file):
    with pytest.raises(ConfigError) as exc:
        load_settings(env_file("ADMIN_IDS=abc\nAI_PROVIDER=custom\n"))
    message = str(exc.value)
    for expected in ("TELEGRAM_BOT_TOKEN", "ADMIN_IDS", "AI_BASE_URL", "AI_MODEL"):
        assert expected in message


def test_parse_json_object_tolerates_fences_and_prose():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Here it is: {"a": {"b": 2}} hope it helps') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        parse_json_object("no json here")


def test_normalize_questions():
    title, qs = normalize_questions({"questions": [
        {"type": "mcq", "question": "Q1", "options": ["a) x", "B. y"], "answer": "A", "points": "2"},
        {"type": "mcq", "question": "Q2", "options": [], "answer": "z"},          # no options -> short
        {"type": "short", "question": "", "answer": "dropped"},
        {"type": "short", "question": "Q4", "answer": "k", "points": 500},
    ]}, max_questions=10)
    assert title == "Assessment"
    assert [(q["type"], q["points"]) for q in qs] == [("mcq", 2), ("short", 1), ("short", 10)]
    assert qs[0]["options"] == ["x", "y"]
    with pytest.raises(ValueError):
        normalize_questions({"questions": []}, 10)


def test_normalize_grades_clamps_and_fills_missing():
    questions = [{"points": 1}, {"points": 3}, {"points": 2}]
    results, overall = normalize_grades({"results": [
        {"number": 2, "awarded": 7, "student_answer": "x", "feedback": "f"},
        {"number": 1, "awarded": -4},
    ]}, questions)
    assert [r["awarded"] for r in results] == [0, 3, 0]
    assert results[2]["student_answer"] == "" and overall == ""


def test_pack_blocks_respects_limit():
    messages = pack_blocks(["a" * 30, "b" * 30, "<b>" + "c" * 100 + "</b>"], limit=70)
    assert messages[0] == "a" * 30 + "\n\n" + "b" * 30
    assert all(len(m) <= 70 for m in messages) and "<b>" not in "".join(messages[1:])


def test_extract_text_formats():
    assert "hello world" in extract_text("a.txt", b"hello world, this is a plain text file")
    with pytest.raises(ExtractionError):
        extract_text("a.xyz", b"whatever content this is")
    with pytest.raises(ExtractionError):
        extract_text("a.pdf", b"%PDF-1.4 broken")

    import docx
    buffer = io.BytesIO()
    document = docx.Document()
    document.add_paragraph("Photosynthesis converts light into chemical energy.")
    document.save(buffer)
    assert "Photosynthesis" in extract_text("notes.docx", buffer.getvalue())
