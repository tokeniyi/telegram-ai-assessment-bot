"""Settings, read from the .env file in the project root (or the process environment)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# provider name -> (default base URL, default model, API key required)
OPENAI_COMPATIBLE_PROVIDERS: dict[str, tuple[str | None, str | None, bool]] = {
    "openai": (None, "gpt-4o-mini", True),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-2.5-flash", True),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", True),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini", True),
    "deepseek": ("https://api.deepseek.com", "deepseek-chat", True),
    "mistral": ("https://api.mistral.ai/v1", "mistral-small-latest", True),
    "ollama": ("http://localhost:11434/v1", "llama3.1", False),
    "custom": (None, None, False),
}
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
SUPPORTED_PROVIDERS = ("anthropic", *OPENAI_COMPATIBLE_PROVIDERS)


class ConfigError(Exception):
    """The .env file is missing something or holds an invalid value."""


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    admin_ids: frozenset[int]
    ai_provider: str
    ai_api_key: str
    ai_model: str
    ai_base_url: str | None
    ai_timeout_seconds: float
    default_num_questions: int
    max_questions: int
    max_file_mb: int
    max_document_chars: int
    show_correct_answers: bool
    allow_resubmission: bool
    database_path: Path
    log_level: str

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings. Values already set in the real environment win over .env."""
    load_dotenv(env_file or PROJECT_ROOT / ".env")

    problems: list[str] = []

    token = _get("TELEGRAM_BOT_TOKEN")
    if not token:
        problems.append("TELEGRAM_BOT_TOKEN is empty (get one from @BotFather)")

    admin_ids: set[int] = set()
    for part in _get("ADMIN_IDS").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            admin_ids.add(int(part))
        except ValueError:
            problems.append(f"ADMIN_IDS has a non-numeric entry: {part!r}")

    provider = _get("AI_PROVIDER", "anthropic").lower()
    api_key = _get("AI_API_KEY")
    model = _get("AI_MODEL")
    base_url = _get("AI_BASE_URL") or None

    if provider not in SUPPORTED_PROVIDERS:
        problems.append(
            f"AI_PROVIDER={provider!r} is not supported. Use one of: {', '.join(SUPPORTED_PROVIDERS)}"
        )
    elif provider == "anthropic":
        model = model or ANTHROPIC_DEFAULT_MODEL
        if not api_key:
            problems.append("AI_API_KEY is empty")
    else:
        default_url, default_model, key_required = OPENAI_COMPATIBLE_PROVIDERS[provider]
        base_url = base_url or default_url
        model = model or default_model or ""
        if key_required and not api_key:
            problems.append("AI_API_KEY is empty")
        if provider == "custom" and not base_url:
            problems.append("AI_BASE_URL is required when AI_PROVIDER=custom")
        if not model:
            problems.append(f"AI_MODEL is required when AI_PROVIDER={provider}")

    if problems:
        raise ConfigError(
            "Configuration problem(s) in .env:\n  - " + "\n  - ".join(problems)
            + "\nSee .env.example for every setting."
        )

    database_path = Path(_get("DATABASE_PATH", "data/bot.db"))
    if not database_path.is_absolute():
        database_path = PROJECT_ROOT / database_path

    max_questions = _int("MAX_QUESTIONS", 30)
    return Settings(
        telegram_bot_token=token,
        admin_ids=frozenset(admin_ids),
        ai_provider=provider,
        ai_api_key=api_key,
        ai_model=model,
        ai_base_url=base_url,
        ai_timeout_seconds=float(_int("AI_TIMEOUT_SECONDS", 180)),
        default_num_questions=min(_int("DEFAULT_NUM_QUESTIONS", 5), max_questions),
        max_questions=max_questions,
        max_file_mb=_int("MAX_FILE_MB", 20),
        max_document_chars=_int("MAX_DOCUMENT_CHARS", 120_000, minimum=500),
        show_correct_answers=_bool("SHOW_CORRECT_ANSWERS", True),
        allow_resubmission=_bool("ALLOW_RESUBMISSION", False),
        database_path=database_path,
        log_level=_get("LOG_LEVEL", "INFO").upper(),
    )
