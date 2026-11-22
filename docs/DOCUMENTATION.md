# Documentation: what changed and what to know

This document covers everything added to the bot, in order, and the things worth knowing before you
run it for real. For setup and day-to-day use, see the [README](../README.md); for every setting, see
[`.env.example`](../.env.example).

## 1. What the bot does

An administrator uploads study material. The AI writes questions, the admin previews and publishes
them to every registered user, users answer in any format, and the AI grades each submission. Admins
get a ranking and a CSV.

## 2. What changed, by pull request

### PR #1: core bot (merged)

- **Material in:** PDF, DOCX, TXT or pasted text (files that already have a text layer).
- **Question generation:** the AI writes questions from the material. A caption steers it
  (number, type, difficulty). The admin previews the draft, with reference answers visible to the
  admin only, then picks **Send to all users**, **Regenerate** or **Discard**.
- **Answering and grading:** users reply with text or a file and tap **Submit**. The submission is
  saved first, then graded, so an answer is never lost if the AI call fails. `/regrade <id>` retries
  failed grading.
- **Results:** `/results [id]` shows a ranking and sends a CSV with per-question scores and raw answers.
- **Storage:** PostgreSQL (`DATABASE_URL`) with `users`, `assessments`, `questions` and `submissions` tables.
- **Providers:** Anthropic plus any OpenAI-compatible API (OpenAI, Gemini, Groq, OpenRouter,
  DeepSeek, Mistral, Ollama, custom). Configured entirely from `.env`.
- **Default provider:** Kilo AI Gateway with `kilo-auto/free`, so a Kilo API key in `AI_API_KEY` is the
  only AI setting needed.

### PR #2: OCR, photos and `/explain` (merged)

**Scanned PDFs and photos** used to be rejected with "needs OCR first". They are now read by a local
OCR pipeline (`bot/ocr/`) built from free, open-source engines. No paid OCR service, no extra key.

- **Per page, only when needed:** a PDF page with a usable text layer is used as is. Pages with a
  broken or sparse text layer (a scanner stamp, a bad font) or none are rendered and OCRed.
- **Image preparation:** EXIF rotation, 90/180/270 uprighting, deskew, shadow removal, denoising,
  contrast stretching, sharpening, upscaling, blank-page and figure detection.
- **Recognition:** RapidOCR (PaddleOCR's PP-OCR models on ONNX Runtime) as the main engine; Tesseract
  and EasyOCR as optional fallbacks for pages the main engine is unsure about.
- **Retry ladder:** a page below `OCR_MIN_CONFIDENCE` is retried on an enhanced image, then with the
  fallback engines; the best attempt wins.
- **Reading order:** columns, tables (cells joined with ` | `), headings and paragraphs.
- **Clean-up:** ligatures, hyphenation, stray specks, repeated headers/footers and page numbers.
- **Report:** the uploader sees progress, then how many pages needed OCR, the confidence, and which
  pages were uncertain. A per-page report is written to the log.
- **Telegram:** photos, image files, scanned PDFs and photo albums (an album is read as the pages of
  one document) work as material (admin) and as answers (users). The bot shows what it read from a
  photo so the user can correct it before submitting.
- **`/explain`:** users can get something explained from a photo, a file, a replied-to message, or a
  direct question. A photo sent while no assessment is open is explained automatically. Turn it off
  with `ALLOW_EXPLAIN=false` (each use is an AI request).
- **Optional AI layer** (uses your configured provider):
  - `OCR_VISION` sends page images to a vision model for handwriting, formulas (as LaTeX) and diagrams.
  - `OCR_AI_CORRECTION` repairs recognition errors in low-confidence text.
  - Both are verified against the local OCR result; a transcription missing the words local OCR was
    sure of, or a correction that rewrites, shortens or extends the text, is discarded.

### Latest commit: free vision models on Kilo

- **Problem:** `kilo-auto/free` is listed with text input only, so it cannot read page images.
- **Change:** with `AI_PROVIDER=kilo` and `OCR_VISION_MODEL` empty, page images go to
  `nvidia/nemotron-3.5-super-vl:free`, then `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` if
  the first request fails. Questions and grading still use `kilo-auto/free`.
- **`OCR_VISION_MODEL` takes a comma-separated list**, tried in order. With other providers it still
  defaults to `AI_MODEL`.
- **Fix:** vision requests no longer change the JSON-mode setting used for question generation.
- **No `.env` edit needed.** To set it explicitly:

  ```
  OCR_VISION_MODEL=nvidia/nemotron-3.5-super-vl:free,nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free
  ```

### PR #3: conversational chat (this PR)

Before, every typed message from a user with an open assessment was stored as an answer, so "hey"
or "what does osmosis mean?" ended up in the graded submission. Now users can talk to the bot like
they would to an AI assistant, and the bot works out what each message is.

**How it works.** Each typed message (private chats, admins included) goes to the AI in one call
that returns an *intent* and, for conversation, the reply:

| Intent | Meaning | What the bot does |
|---|---|---|
| `chat` | Greeting, small talk, a question about a topic, the bot or a question of the assessment, talk about results | Sends the AI's reply. Nothing is stored. |
| `answer` | Part of the user's answers (`1. B`, a statement answering a question, a continuation) | Saves it to the draft, as before, with **Submit / Undo / Discard** buttons |
| `review` | "I'm done", "please review my answers", "grade me" | Shows how many messages are saved and a **Submit** button |

- **Review asks for a tap on Submit instead of grading at once.** Submitting is final unless
  `ALLOW_RESUBMISSION=true`, and a wrong guess about intent should not use up someone's only attempt.
- **When unsure, the AI is told to pick `answer`.** A wrongly saved message is fixed with the new
  **Undo** button (removes the last saved message). A wrongly dropped answer would be lost silently.
- **What the AI knows.** The open assessment's questions (never the reference answers, so it cannot
  leak them and is told to give hints instead), how many answer messages are saved, the user's latest
  graded result (reference answers only if `SHOW_CORRECT_ANSWERS=true`, same as the result message),
  and the last `CHAT_HISTORY_TURNS` exchanges. Users can therefore ask "why did I lose points on Q2?".
- **Messages are handled one at a time per user**, so answers sent in quick succession are saved in order.
- **If the AI is unavailable**, text that looks like an answer (`1. B`, `a) ...`) or that follows
  already saved answers is still saved; anything else gets "could not reach my AI". No answer is lost.
- **Unchanged:** files and photos from non-admins are still answers; commands, `/explain` and the admin
  upload flow work as before. Set `ALLOW_CHAT=false` to restore the old behaviour (all text is an answer).

New files: `bot/handlers/chat.py` (routing), `converse()` and `CHAT_SYSTEM` in `bot/ai/service.py`,
`tests/test_chat.py`. `bot/handlers/student.py` lost its text handler to `chat.py`.

### PR #7: persistent user sessions

Before, a user's unfinished work lived in the bot's memory (`context.user_data`), so every restart
or Railway redeploy lost the answers typed so far, the assessment picked with `/pending`, and the
chat history. Now these are stored in PostgreSQL and the user simply carries on, including tapping a
**Submit / Undo / Discard** button that the previous version of the bot sent.

| State | Before | Now |
|---|---|---|
| Answers typed but not submitted | memory | `draft_answers` table, one row per saved message |
| Assessment picked with `/pending` | memory | `user_sessions.target_assessment_id` |
| Chat history | memory | `user_sessions.chat_history` (JSON, trimmed to `CHAT_HISTORY_TURNS`) |
| "Grading in progress" flag | memory | unchanged, see below |
| Submissions and grades | PostgreSQL | unchanged |

**Submitting happens exactly once.** `Database.submit_draft` claims the draft by deleting it
(`DELETE … RETURNING`) and creates the submission in the same transaction. A double tap, a retried
Telegram update, or two bot versions running side by side during a deploy can therefore create at
most one submission: the others find the draft already gone and get "already submitted".

**Per-user locking.** Every change to a user's draft or history first locks that user's
`user_sessions` row, so changes from several processes are applied one after the other. The
`asyncio.Lock` in `chat.py` stays: it keeps a quick series of messages in order within one process.

**What is deliberately still in memory.** The `grading`, `reading` and `explaining` flags only stop
one process from starting the same slow job twice for a user; they are not correctness guards, so
losing them on restart is harmless. A grading interrupted by a restart leaves its submission
`pending`; `/regrade` retries it. (Stage 3 below resumes it automatically.)

**One behaviour change.** Tapping an assessment in `/pending` that you already have answers saved
for keeps them and shows the buttons again (before, it wiped them). Picking a *different*
assessment still discards the draft, as before: a user has one draft at a time.

The new tables are created on start (`CREATE TABLE IF NOT EXISTS`); existing data is not touched.
New file: `tests/test_sessions.py` (restart, resume, undo, cancel, switching, exactly-once submit).

### Stage 2: each Telegram update is handled once

Telegram sends an update again when the bot did not confirm it: the process crashed or was
redeployed right after fetching a batch, or two bot versions polled at the same time during a
Railway deploy. Before, such a copy was handled again: an answer saved twice, the same upload sent
to the AI twice, the same photo read and explained twice.

Now the first handler of all (`bot/handlers/dedupe.py`, group -2) records the update's id in the new
`processed_updates` table (`PRIMARY KEY (bot_id, update_id)`, `INSERT … ON CONFLICT DO NOTHING`).
Only the process whose insert succeeds handles the update; every copy stops before any other handler,
in this process or another one.

- **At most once, not exactly once.** The id is recorded *before* handling. If a crash cuts handling
  short and Telegram resends the update, the copy is skipped and the user sends that message again.
  This was chosen over repeating half-done work (a second draft line, a second broadcast).
- **Fails open.** If the database cannot record the id, the update is handled anyway and a warning is
  logged; the bot does not go silent while PostgreSQL has a hiccup.
- **Double taps are a different thing.** Two taps on Submit are two updates with different ids, so
  this guard does not merge them; `Database.submit_draft` (Stage 1) still makes them one submission.
- **Clean-up.** Telegram keeps an unconfirmed update for at most 24 hours, so records older than 48
  hours are deleted at start-up and after every 1000 updates.
- `bot_id` is part of the key because a new bot token starts its own numbering.

The table is created on start (`CREATE TABLE IF NOT EXISTS`); existing data is not touched.
New files: `bot/handlers/dedupe.py`, `tests/test_updates.py`. The test harness now shares one update
counter between harnesses that stand for a restarted bot, as Telegram does.

### Stage 3: sending and grading survive a restart

Sending and grading used to run only inside the handler that started them, so a restart in the
middle lost the rest. Both now run from rows in PostgreSQL that any bot process can finish.

- **Send queue.** "Send to all users" publishes the draft and writes one `deliveries` row per user
  in a single transaction (`UPDATE … WHERE status = 'draft' RETURNING`), so two quick taps, or two
  bot versions, publish it once. Each row is claimed with a 5-minute lease, sent, and marked
  `sent`, `blocked` or (after 3 tries) `failed`. The publisher gets the "sent to N users" report
  once, from whichever process finishes.
- **Grading.** A new submission is leased to the process grading it. If that process stops, the
  lease runs out and another process grades it, at most 3 times; then it is `failed` and the admins
  are told to `/regrade`. A result that was graded but not delivered is sent once more after 5
  minutes.
- **On start and every 30 seconds** the bot finishes leftover sending and grading.
- **Also closed:** Discard or Regenerate racing with Send, `/close` during sending (the rest is
  not sent), and `/regrade` on a submission being graded right now.
- **Upgrade.** New columns and the `deliveries` table are added on start. Work done by the previous
  version is not redone: old results are not resent, old assessments are not re-reported.

Details, decisions and tests: [docs/implementations/phase-03](implementations/phase-03/summary.md).

## 3. Things to know

### Unverified: the Kilo model IDs
The two free vision model IDs come from web research (Kilo's model page for Nemotron 3.5; third-party
catalogues for Nano Omni). **No live request to Kilo was made** while developing this, because its API
was unreachable from the build environment. Before relying on it, send one scanned page or photo and
check that the report does not say the AI could not look at the pages.

### Free tier limits and privacy
- **Rate limits:** sources disagree on whether Kilo's free tier allows 200 requests per hour or per day.
  Each page sent to vision is one request, capped at 12 per file by `OCR_VISION_MAX_PAGES`.
- **Privacy:** Kilo warns that free providers may log or retain prompts and outputs, and one source says
  the free NVIDIA endpoints may use them for training. Page images of your material, and users'
  answers, may therefore be kept. Use a paid model or another provider for sensitive material.
- **Withdrawal:** free models are removed without notice. If every listed vision model fails, the bot
  keeps the local OCR text and says in the report that the AI could not look at the pages. Pick a
  current image-capable model from Kilo's list and set `OCR_VISION_MODEL`, or set `OCR_VISION=off`.
- **Quality varies:** `kilo-auto/free` picks a different free model per request, so question and
  grading quality can vary.

### What local OCR can and cannot do
- Built for printed text. Handwriting, formulas and diagrams need the AI vision layer; without it,
  formulas come out as plain characters, diagrams are skipped and messy handwriting may be misread.
- Bundled models read Latin-script languages and Chinese. For other scripts install Tesseract language
  packs, set `OCR_TESSERACT_LANGS` and put `tesseract` first in `OCR_ENGINES`.
- EasyOCR (optional) reads English only and pulls in PyTorch (about 2 GB). HEIC photos need
  `pip install pillow-heif` (untested).
- A picture inside a page that also has a full text layer is not OCRed.
- Telegram compresses photos to 1280 px. For small print, send the image as a file (attach, then File).

### Performance and memory (4 CPU cores, no GPU)
- Photo: 1 to 2.5 s. Dense scanned A4 page: about 2 s, so 100 pages take 3 to 4 minutes, with progress
  shown in the chat. Pages with a text layer cost no OCR time.
- Memory: about 0.9 GB with the default `OCR_WORKERS=2`, 0.6 GB with 1, 1.5 GB with 4. On a 1 GB
  server use `OCR_WORKERS=1`.

### Chat

- **Cost and limits:** every typed message is one AI request, including "thanks". On the free Kilo
  model that counts against its rate limit, and free providers may log prompts, so chat text and
  answers may be retained by them. Set `ALLOW_CHAT=false` to avoid the extra calls.
- **The classification is a judgement by the model**, so it can be wrong, especially with the free
  auto model, whose quality varies. That is why wrong answers are undoable and review needs a tap.
  The classification has only been tested with a scripted stand-in, not with a live model. Try
  "hey", a question, `1. B`, and "I'm done" on your own bot before relying on it.
- **A short answer without numbering** (just "Mitochondria") is judged from context; to be safe, number answers.
- **History:** chat history and drafts are stored in PostgreSQL and survive restarts. Only the last
  `CHAT_HISTORY_TURNS` (8) exchanges are kept and sent to the AI.
- **The AI is told to give hints, not the reference answers.** That is an instruction to the model,
  not a guarantee, but the reference answers are never put in the prompt of an open assessment.

### Limits and behaviour
- Private chats only; the bot does not post to groups or channels.
- Answers typed but not yet submitted are stored in PostgreSQL and survive restarts (PR #7).
- Material longer than `MAX_DOCUMENT_CHARS` (120000) is cut and the admin is told.
- Page caps per file: `OCR_MAX_PAGES` (150, admins), `OCR_MAX_PAGES_USER` (15, everyone else).
- Resubmission is off by default (`ALLOW_RESUBMISSION`); when on, the latest attempt counts.
- Users see reference answers after grading unless `SHOW_CORRECT_ANSWERS=false`.
- Admins also receive published assessments, so you can test the whole flow from one account.
- Setting the real environment variable overrides the same name in `.env`.

## 4. Commands

| Who | Command | Purpose |
|---|---|---|
| Everyone | `/start`, `/help`, `/myid` | Register, help, show your Telegram ID |
| User | `/pending`, `/myresults`, `/cancel` | Open assessments, scores, discard typed answers |
| User | `/explain` | Explain a photo, file, replied message or question |
| User | *(just type)* | Chat with the AI, send answers, or say you are done to get a Submit button |
| Admin | `/generate` | Reply to a message, file or photo to use it as material |
| Admin | `/assessments`, `/results [id]`, `/close <id>`, `/regrade <id>`, `/users`, `/status` | Manage and review |
| Admin | `/resend <id>` | Send an open assessment again to users whose message failed (Stage 8) |
| Admin | `/deadline <id> [when]`, `/settings <id>` | Deadline and reminders; per-assessment reference answers and resubmission (Stage 7) |

## 5. Settings

Added by PR #3: `ALLOW_CHAT`, `CHAT_HISTORY_TURNS`. The rest were added by PR #2.

| Setting | Default | Notes |
|---|---|---|
| `OCR_ENABLED` | `true` | `false` refuses files with no text layer |
| `OCR_ENGINES` | `rapidocr,tesseract` | Tried in order; missing engines are skipped |
| `OCR_WORKERS` | 2 | Pages read in parallel |
| `OCR_MAX_PAGES` / `OCR_MAX_PAGES_USER` | 150 / 15 | Per-file page caps |
| `OCR_DPI` | 170 | Raise to 220 for very small print |
| `OCR_MIN_CONFIDENCE` | 80 | Percent; below this a page is retried and reported |
| `OCR_TESSERACT_LANGS` | `eng` | e.g. `eng+fra` |
| `OCR_VISION` | `auto` | `off`, `auto` (uncertain or formula/figure pages), `on` (every scanned page) |
| `OCR_VISION_MODEL` | empty | Comma-separated list. Empty: two free Nemotron models on Kilo, else `AI_MODEL` |
| `OCR_VISION_MAX_PAGES` | 12 | Page images sent to the AI per file |
| `OCR_AI_CORRECTION` | `auto` | `off`, `auto` (low-confidence pages), `on` |
| `ALLOW_EXPLAIN` | `true` | Enables `/explain` |
| `ALLOW_CHAT` | `true` | `false`: no chat, every typed message is an answer (old behaviour) |
| `CHAT_HISTORY_TURNS` | 8 | Earlier exchanges the AI remembers per user; 0 for none |
| `SHOW_CORRECT_ANSWERS` / `ALLOW_RESUBMISSION` | `true` / `false` | Defaults; `/settings <id>` overrides them per assessment (Stage 7) |
| `TIMEZONE` | `UTC` | Time zone deadlines are typed and shown in (Stage 7) |
| `AUTO_REGRADE_ATTEMPTS` | 3 | Times the bot retries failed grading by itself once the AI answers; 0 for none (Stage 8) |
| `REMINDER_HOURS` | 24 | Hours before a deadline to remind students who have not submitted; 0 for none (Stage 7) |

## 6. Dependencies

- `requirements.txt` adds `rapidocr`, `onnxruntime`, `opencv-python-headless`, `numpy`, `Pillow`,
  `pypdfium2`. The OCR models ship inside the `rapidocr` package; nothing is downloaded at run time.
- `requirements-ocr-extra.txt` (optional): `pytesseract` (also needs the `tesseract` program) and
  EasyOCR (commented out).
- `requirements-dev.txt`: test dependencies.

## 7. Tests

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest
```

`tests/test_sessions.py` covers resuming after a simulated restart (a second application on the same
database), undo, cancel, switching assessments, and that repeated or concurrent submits create one
submission.

`tests/test_chat.py` covers intent routing, mixed chat and answers, review, undo, history limits, answer
secrecy, AI failure, malformed replies, rapid messages and `ALLOW_CHAT=false`.

Tests run offline with a fake Telegram API and a fake AI, so no token, key or network is needed.
OCR tests render deliberately damaged pages (rotated, tilted, shadowed, blurred, noisy, low
resolution, compressed, two-column, tables, scanned and mixed PDFs) and check what is read. Tesseract
and EasyOCR tests run only where those engines are installed. The live Kilo vision call is not covered
by any test.

## 8. Before you merge: checklist

- [ ] `pip install -r requirements.txt` and `python -m pytest` pass on your machine.
- [ ] `.env` has `TELEGRAM_BOT_TOKEN`, `ADMIN_IDS`, `AI_API_KEY`.
- [ ] Send a scanned page or photo and confirm the report does not say the AI could not look at the pages.
- [ ] Decide whether the free-tier privacy terms are acceptable for your material.
- [ ] Back up `data/bot.db` if you already have data (the schema is unchanged).
- [ ] Chat with your bot: "hey", a question about the topic, `1. B`, then "I'm done". Check each is handled correctly.
- [ ] After merging PR #7: answer two questions, let Railway redeploy (or restart the service), then
      send a third answer and tap Submit. The submission should contain all three.
