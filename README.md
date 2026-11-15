# telegram-ai-assessment-bot

A Telegram bot that turns study material into graded assessments:

1. An **administrator** sends the bot a PDF, DOCX or TXT file (or pasted notes).
2. The **AI** writes questions from it. The admin previews them and sends them to all users.
3. **Users** reply with their answers in any format and tap Submit.
4. The bot **stores** the answers, the AI **grades** them, and each user gets a score with feedback.
5. Admins see the scores in the chat and can download a CSV.

## Setup

Requires Python 3.10 or newer.

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Fill in `.env`, then start the bot:

```bash
python main.py
```

The bot reads all configuration from `.env` in the project folder, both locally and on a server.
Variables set in the real environment take priority over the file.

### What to put in `.env`

| Setting | Required | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Token from [@BotFather](https://t.me/BotFather) |
| `ADMIN_IDS` | yes, to upload | Comma-separated Telegram user IDs of the admins |
| `AI_API_KEY` | yes | Your [Kilo](https://kilo.ai) API key |
| `AI_PROVIDER` | no | Defaults to `kilo`. Others: `anthropic`, `openai`, `gemini`, `groq`, `openrouter`, `deepseek`, `mistral`, `ollama`, `custom` |
| `AI_MODEL` | no | Defaults to `kilo-auto/free`, Kilo's auto model that routes each request to a free model |
| `AI_BASE_URL` | only for `custom` | URL of any OpenAI-compatible API |

`.env.example` lists the remaining optional settings (question counts, file size limit,
whether users see reference answers, resubmission, database path, log level).

**About the free Kilo model:** `kilo-auto/free` costs nothing, but Kilo decides which free model
answers each request, so question and grading quality can vary between requests, and free models
are rate limited. Kilo also warns that free providers may log prompts and outputs, so the uploaded
material and users' answers may be retained by them. To use a specific or paid Kilo model, set
`AI_MODEL` to its ID from the Kilo model list.

**Finding your admin ID:** start the bot with `ADMIN_IDS` empty, send it `/myid`, put the number in
`ADMIN_IDS`, and restart.

## Using the bot

Everyone who sends `/start` is registered and receives published assessments. Admins receive
them too, so you can test the whole flow from a single account.

**Admin**

- Send a file. A caption steers the result, e.g. `10 multiple-choice questions on chapter 2, hard`.
- Review the draft (reference answers are shown to you only), then tap **Send to all users**,
  **Regenerate** or **Discard**.
- `/generate` — reply with this to a text message to use that text as the material
- `/assessments` — list assessments and their status
- `/results [id]` — ranking plus a CSV with per-question scores and raw answers
- `/close <id>` — stop accepting answers
- `/regrade <id>` — grade submissions whose grading failed (for example, the AI provider was down)
- `/users` — number of registered users

**User**

- Reply with answers as one or several messages, or as a TXT/PDF/DOCX file, then tap **Submit**.
- `/pending` — open assessments; pick one to answer
- `/myresults` — scores so far
- `/cancel` — discard the answers typed so far

## Project layout

```
main.py                 entry point
bot/config.py           reads and validates .env
bot/app.py              builds the Telegram application
bot/db.py               SQLite storage (users, assessments, questions, submissions)
bot/extract.py          PDF / DOCX / text extraction
bot/ai/providers.py     Anthropic and OpenAI-compatible providers
bot/ai/service.py       prompts, question generation, grading, reply validation
bot/handlers/           admin, user and shared Telegram handlers
tests/                  offline tests with a fake Telegram API and a fake AI
```

Data is stored in `data/bot.db` (SQLite, created on first run). Submissions are saved before
grading, so an answer is never lost if the AI call fails.

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest
```

The tests run without a bot token, an API key or network access.

## Limits

- Scanned PDFs (images of pages) contain no text and are rejected; they need OCR first.
- Material longer than `MAX_DOCUMENT_CHARS` is cut at that length, and the admin is told.
- Answers typed but not yet submitted are kept in memory and are lost if the bot restarts.
- The bot works in private chats only; it does not post to groups or channels.
