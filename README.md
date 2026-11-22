# telegram-ai-assessment-bot

A Telegram bot that turns study material into graded assessments:

1. An **administrator** sends the bot a PDF, DOCX or TXT file, a scanned PDF, photos of notes, or pasted text.
2. The **AI** writes questions from it. The admin previews them and sends them to all users.
3. **Users** reply with their answers in any format (typed, a file, or a photo of handwriting) and tap Submit.
4. The bot **stores** the answers, the AI **grades** them, and each user gets a score with feedback.
5. Admins see the scores in the chat and can download a CSV.

Full notes on what changed and what to know before running it: [docs/DOCUMENTATION.md](docs/DOCUMENTATION.md).

Planned work, stage by stage: [ROADMAP.md](ROADMAP.md).

## Setup

Requires Python 3.10 or newer and a PostgreSQL database (12 or newer). Create an empty one first,
for example `createdb assessment_bot`; the bot creates its tables on first start.

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
| `DATABASE_URL` | yes | PostgreSQL URL, e.g. `postgresql://user:password@localhost:5432/assessment_bot` |
| `AI_API_KEY` | yes | Your [Kilo](https://kilo.ai) API key |
| `AI_PROVIDER` | no | Defaults to `kilo`. Others: `anthropic`, `openai`, `gemini`, `groq`, `openrouter`, `deepseek`, `mistral`, `ollama`, `custom` |
| `AI_MODEL` | no | Defaults to `kilo-auto/free`, Kilo's auto model that routes each request to a free model |
| `AI_BASE_URL` | only for `custom` | URL of any OpenAI-compatible API |

`.env.example` lists the remaining optional settings (question counts, file size limit,
whether users see reference answers, resubmission, OCR, database path, log level and format).

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

- Send a file, a scanned PDF or a photo of notes. Several photos sent together as one album are read
  as the pages of one document. A caption steers the result, e.g.
  `10 multiple-choice questions on chapter 2, hard`.
- For scans and photos the bot shows its progress, then how the file was read: how many pages needed
  OCR, the OCR confidence, and which pages it was unsure about.
- Review the draft (reference answers are shown to you only), then tap **Send to all users**,
  **Regenerate** or **Discard**.
- `/generate` — reply with this to a text message, file or photo to use it as the material
- `/assessments` — list assessments and their status
- `/results [id]` — ranking plus a CSV with per-question scores and raw answers
- `/close <id>` — stop accepting answers
- `/deadline <id> <when>` — close an assessment by itself at a time, e.g. `2d`, `tomorrow 17:00`,
  `2026-11-30 17:00` (in `TIMEZONE`), or `off`. Students who have not submitted are reminded
  `REMINDER_HOURS` (24) hours before, and you are told how many submitted once it closes.
- `/settings <id>` — the deadline, and whether students of this assessment see reference answers and
  may resubmit (by default `SHOW_CORRECT_ANSWERS` and `ALLOW_RESUBMISSION`); buttons change them.
  The draft preview has a **⚙️ Settings** button.
- `/regrade <id>` — grade submissions whose grading failed (for example, the AI provider was down).
  The bot also retries them by itself once the provider answers again (`AUTO_REGRADE_ATTEMPTS`, 3),
  and tells you when it did.
- `/resend <id>` — send an open assessment again to the users it could not reach (a timeout, or 3
  failed attempts). Some may already have it, so they may receive it twice.
- `/users` — number of registered users
- `/status` — grading waiting or failed, assessment messages queued or failed, and whether the AI
  provider answers right now (see [Monitoring](#monitoring))

**User**

- Reply with answers as one or several messages, as a TXT/PDF/DOCX file, or as a photo of handwritten
  answers, then tap **Submit**. The bot shows what it read from a photo so it can be corrected first.
- `/explain` — get something explained. Send a photo or file with the caption `/explain` (a question
  can follow, e.g. `/explain why is this negative?`), reply `/explain` to a message, or ask directly:
  `/explain what is osmosis?`. A photo sent while no assessment is open is explained automatically.
- **Just chat.** Say "hey", ask what a question means, or ask about a topic and the AI answers like an
  assistant. The AI also tells answers apart from conversation: `1. B` is saved as an answer, and
  "I'm done, please review my answers" brings up the **Submit** button. If a message is saved by
  mistake, tap **Undo** under it. It never reveals the reference answers of an open assessment, and it
  can discuss your latest result. `ALLOW_CHAT=false` turns this off (every typed message is then an answer).
- `/pending` — open assessments (with their deadlines); pick one to answer. Before a deadline the bot
  reminds you if you have not submitted, with a button to open the questions or submit what you saved.
- `/myresults` — scores so far
- `/cancel` — discard the answers typed so far
- `/checkins [on|off|pause]` — study check-ins. With your first `/start` the bot asks whether you
  want them (students who registered earlier are asked once by message). If you say yes, it sends you a short AI-written message every `CHECKIN_DAYS` (3) days at
  `CHECKIN_HOUR` (17:00, in `TIMEZONE`). The message suggests something to review and invites you
  to reply if you're stuck. A reply goes to the chat like any other message. `pause` stops them for
  a week. The bot sends them less often if you don't answer, and skips a day when you just used the
  bot or got a deadline reminder.

**Guidance.** You don't need to remember the commands. A mistyped command gets "did you mean"
with the closest commands you may use. When a command cannot do what you asked, the reply says why
(a wrong number, a draft that is not published yet, the AI not answering) and what to do instead,
with real assessment numbers you can tap to copy. Safe next steps also come as buttons (📊 Results,
🔁 Retry grading, 📝 Open assessments). Students are never shown administrators' commands, and
grading that the bot is already retrying is reported as "no action needed".

## Project layout

```
main.py                 entry point
bot/config.py           reads and validates .env
bot/app.py              builds the Telegram application
bot/runtime.py          start-up, polling or webhook, and the clean shutdown on SIGTERM
bot/web.py              the /health endpoint and the webhook receiver
bot/db.py               PostgreSQL storage (users, assessments, questions, submissions)
bot/extract.py          PDF / DOCX / text extraction for files that have a text layer
bot/ocr/                OCR pipeline for scanned PDFs and images (see below)
bot/ai/providers.py     Anthropic and OpenAI-compatible providers
bot/ai/service.py       prompts, question generation, grading, reply validation
bot/commands.py         the command registry: menus, /help, "did you mean" and suggestions
bot/guidance.py         replies that explain a problem and suggest the next command (real ids)
bot/deadlines.py        reading and showing deadlines (/deadline), in TIMEZONE
bot/checkins.py         study check-ins: when they are due, and what the AI is told about the student
bot/alerts.py           tells the admins when the AI provider is down or grading keeps failing
bot/logs.py             logging set-up and structured log events (text or JSON lines)
bot/handlers/           admin, user and shared Telegram handlers; uploads.py routes every file
                        and photo, reading.py downloads and reads them, explain.py is /explain,
                        chat.py routes typed messages (chat, answer or review),
                        guide.py answers unknown commands and runs command buttons,
                        work.py sends assessments and grades in the background
tests/                  offline tests with a fake Telegram API and a fake AI
```

Data is stored in PostgreSQL (set `DATABASE_URL`; the tables are created on first run). Submissions are saved before
grading, so an answer is never lost if the AI call fails. Each Telegram update is recorded before it
is handled, so one that Telegram sends again (after a crash, or during a deploy) is not handled twice.

## Deploying (Railway)

The bot runs as one service from the `Dockerfile`. During a deploy, Railway starts the new
version while the old one still runs, then stops the old one. Three service settings make that
handover clean; set them in the Railway dashboard (service → Settings / Variables):

| Setting | Value | Why |
|---|---|---|
| Healthcheck path | `/health` | Railway switches to the new version only once it answers 200, i.e. it is receiving updates and reaches the database. Railway sets `PORT`; the bot listens on it. |
| `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` | `60` | Time between SIGTERM and the forced kill of the old version. Railway's default is 0, which kills it at once and cuts off grading in progress. Keep it above `SHUTDOWN_GRACE_SECONDS` (50 by default). |
| `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS` | `0` (or unset) | How long the old version keeps running after the new one is active. Longer means more time with two bots fetching updates. |

On SIGTERM the bot stops fetching updates, reports unhealthy, finishes the updates and the
message or grading in hand (at most `SHUTDOWN_GRACE_SECONDS`), then exits. Work cut off by a kill is
picked up by the next version (see Stage 3).

**Optional: webhook instead of polling.** With polling, both versions fetch updates for the few
seconds they overlap. With `WEBHOOK_URL` set to the service's public HTTPS address (for example
`https://<service>.up.railway.app`, from service → Settings → Networking → Generate Domain),
Telegram sends each update to `/telegram`, and Railway routes it to one version only. The request
must carry a secret (`WEBHOOK_SECRET`, derived from the bot token if not set). Removing
`WEBHOOK_URL` goes back to polling; the webhook is removed on the next start.

## OCR: scanned PDFs and photos

The bot has its own OCR pipeline built from free, open-source engines that run on your server.
There is no paid OCR service and no extra API key.

```
upload ─┬─ TXT / DOCX ───────────────────────────────────────── text
        ├─ PDF ── page has a usable text layer? ── yes ──────── text   (no OCR, instant)
        │                └─ no: render the page ─┐
        └─ photo / image ────────────────────────┤
                                                 ▼
   1. locate text · turn the page upright (90/180/270) · measure tilt
   2. read every line from the full-resolution page
   3. unsure? → retry on an enhanced page (shadows flattened, denoised,
                contrast stretched) → fallback engines
   4. reading order: columns, tables, headings, paragraphs
   5. clean-up, per-page confidence, low-confidence warning
   6. AI layer (optional): vision model for handwriting, formulas, diagrams;
                           text model repairs low-confidence pages
```

| Stage | File | What it does |
|---|---|---|
| Detection | `bot/ocr/detector.py` | Tells PDFs and images apart by content. Classifies each PDF page's text layer as good, sparse (a scanner stamp, a broken font) or absent, so OCR runs only on pages that need it. |
| PDF | `bot/ocr/pdf_handler.py` | Reads text layers (pypdf) and renders pages to images (PDFium). |
| Preprocessing | `bot/ocr/preprocess.py` | EXIF rotation, deskew, shadow removal, denoising, CLAHE contrast, sharpening, upscaling, blank-page and figure detection (OpenCV). |
| Recognition | `bot/ocr/recognizer.py` | The engines behind one interface, and the geometry used to upright a page. |
| Layout | `bot/ocr/layout.py` | Reading order: splits columns (also under a full-width title), reads tables row by row with ` \| ` between cells, separates headings and paragraphs. |
| Clean-up | `bot/ocr/postprocess.py` | Ligatures, hyphenation, stray specks, repeated headers/footers and page numbers; flags pages with formulas. |
| AI layer | `bot/ocr/language_model.py` | Vision transcription and text correction, each checked against the local OCR result. |
| Pipeline | `bot/ocr/pipeline.py` | Runs the stages, the retry ladder, parallel pages, and builds the per-page report. |

**Engines.** The main engine is [RapidOCR](https://github.com/RapidAI/RapidOCR): PaddleOCR's PP-OCR
detection and recognition models running on ONNX Runtime. It needs neither PaddlePaddle nor PyTorch,
and its models ship inside the pip package, so nothing is downloaded at run time. Tesseract and
EasyOCR are optional fallbacks (`requirements-ocr-extra.txt`); they are used only for pages the main
engine is unsure about. The bundled models read Latin-script languages and Chinese. For other
scripts, install the Tesseract language packs, set `OCR_TESSERACT_LANGS` and put `tesseract` first in
`OCR_ENGINES`.

**Confidence.** Every OCRed page gets a confidence between 0 and 1 (the length-weighted confidence of
its lines). A page below `OCR_MIN_CONFIDENCE` is retried on an enhanced image, then with the fallback
engines, and the best attempt wins. If it is still below the threshold, the uploader is told which
pages, and the per-page report (`page`, `source`, `engine`, `confidence`) is written to the log.

**The AI layer** uses the AI provider you already configured, so it follows that provider's cost and
privacy terms. It is what reads handwriting, writes formulas as LaTeX and describes diagrams; local
OCR alone is built for printed text, and returns formulas as plain characters.
- `OCR_VISION` sends page images to the model named in `OCR_VISION_MODEL`, which must accept images.
  With Kilo it defaults to two free vision models, tried in order:
  `nvidia/nemotron-3.5-super-vl:free`, then `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`
  (`kilo-auto/free`, which writes the questions, does not take images). With other providers it
  defaults to `AI_MODEL`. Several models can be listed, separated by commas. If none of them can
  read a page, the bot says so in the report and keeps the local text. Free models are rate
  limited, get withdrawn without notice, and their providers may log what is sent to them.
- `OCR_AI_CORRECTION` repairs recognition errors (`thc m1tochondria` → `the mitochondria`).
- Both are verified: a transcription that does not contain the words local OCR was sure of is
  discarded, and a correction that rewrites, shortens or extends the text is discarded. A page that
  only the AI could read (local OCR found nothing to compare with) is used but cannot be verified.

**Speed and memory** (measured on 4 CPU cores, no GPU): a photo takes 1 to 2.5 seconds; a dense
scanned A4 page about 2 seconds, so a 100-page scan takes 3 to 4 minutes, with progress shown in the
chat. Pages with a text layer take no OCR time. The bot process uses about 0.9 GB of memory with the
default 2 workers, 0.6 GB with `OCR_WORKERS=1` and 1.5 GB with 4.

**Photos.** Telegram compresses photos to at most 1280 pixels. That is fine for a page of notes, but
for small print send the picture as a file (attach → File) so it arrives at full resolution.

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest
```

The tests need a running PostgreSQL server but no bot token, API key or network access. They connect to
`TEST_DATABASE_URL` (default `postgresql://postgres:postgres@localhost:5432/bot_test`; create that
database first) and give every test its own temporary schema, which is dropped afterwards. The OCR tests render pages with
real damage (rotated, tilted, shadowed, blurred, noisy, low resolution, heavily compressed, two
columns, tables, scanned and mixed PDFs) and check what the pipeline reads from them. Tests for
Tesseract and EasyOCR run only where those engines are installed.

## Monitoring

- **`/status`** (admins) answers from the database, so it covers every running bot process. It
  checks the AI provider by listing its models, which uses no tokens, and shows how the latest
  real requests went.
- **Alerts.** After 3 AI requests in a row fail to reach the provider (network, error status, rate
  limit, rejected key), the admins get one 🚨 message, and one ✅ message when a request gets
  through again, listing what waits for `/regrade`. A reply that arrives but cannot be used does
  not count as an outage. The first grading failure is reported at once. Later failures are
  reported together, at most every 15 minutes, and held during an outage (the ✅ message reports
  them).
- **Logs.** Sending, grading, results, OCR and AI requests write one event per line, e.g.
  `grading.failed submission=12 assessment=3 user=42 attempt=1 seconds=4.2 error="…"`. With
  `LOG_FORMAT=json` each line is a JSON object, and Railway's log explorer can filter on any field
  (`@event:grading.failed`, `@assessment:3`, `@level:error`). Logs go to stdout. Events carry ids,
  counts, timings and error messages, never answers or document text.

## Limits

- Handwriting, formulas and diagrams are read by the AI vision layer, not by local OCR. They need
  an AI model that accepts images (`OCR_VISION`, `OCR_VISION_MODEL`). Without one, formulas come out
  as plain characters, diagrams are skipped and messy handwriting may be misread or not read.
- EasyOCR, when enabled, reads English only. HEIC photos need `pip install pillow-heif` (not tested).
- A picture inside a page that also has a full text layer is not OCRed; that page's text layer is used.
- Material longer than `MAX_DOCUMENT_CHARS` is cut at that length, and the admin is told.
- Answers typed but not yet submitted, the assessment picked with `/pending` and recent chat are
  stored in PostgreSQL, so a restart or redeploy does not lose them. Sending a published assessment
  and grading a submission also survive a restart: on start (and every 30 seconds) the bot sends the
  assessment to the students still waiting for it and grades submissions whose grading was cut off.
  A submission whose grading fails (or is cut off 3 times) is kept; the bot retries it by itself
  once the AI provider answers again, and `/regrade` retries it at once.
- Answers stop at the deadline itself; the assessment is marked closed, and its publisher told, within
  30 seconds. Each reminder is sent at most once: a bot stopped just as it sends one does not repeat it.
  Answers saved but not submitted by the deadline are not submitted automatically.
- The bot works in private chats only; it does not post to groups or channels.
