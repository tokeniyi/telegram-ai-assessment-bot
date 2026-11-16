# telegram-ai-assessment-bot

A Telegram bot that turns study material into graded assessments:

1. An **administrator** sends the bot a PDF, DOCX or TXT file, a scanned PDF, photos of notes, or pasted text.
2. The **AI** writes questions from it. The admin previews them and sends them to all users.
3. **Users** reply with their answers in any format (typed, a file, or a photo of handwriting) and tap Submit.
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
whether users see reference answers, resubmission, OCR, database path, log level).

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
- `/regrade <id>` — grade submissions whose grading failed (for example, the AI provider was down)
- `/users` — number of registered users

**User**

- Reply with answers as one or several messages, as a TXT/PDF/DOCX file, or as a photo of handwritten
  answers, then tap **Submit**. The bot shows what it read from a photo so it can be corrected first.
- `/explain` — get something explained. Send a photo or file with the caption `/explain` (a question
  can follow, e.g. `/explain why is this negative?`), reply `/explain` to a message, or ask directly:
  `/explain what is osmosis?`. A photo sent while no assessment is open is explained automatically.
- `/pending` — open assessments; pick one to answer
- `/myresults` — scores so far
- `/cancel` — discard the answers typed so far

## Project layout

```
main.py                 entry point
bot/config.py           reads and validates .env
bot/app.py              builds the Telegram application
bot/db.py               SQLite storage (users, assessments, questions, submissions)
bot/extract.py          PDF / DOCX / text extraction for files that have a text layer
bot/ocr/                OCR pipeline for scanned PDFs and images (see below)
bot/ai/providers.py     Anthropic and OpenAI-compatible providers
bot/ai/service.py       prompts, question generation, grading, reply validation
bot/handlers/           admin, user and shared Telegram handlers; uploads.py routes every file
                        and photo, reading.py downloads and reads them, explain.py is /explain
tests/                  offline tests with a fake Telegram API and a fake AI
```

Data is stored in `data/bot.db` (SQLite, created on first run). Submissions are saved before
grading, so an answer is never lost if the AI call fails.

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
- `OCR_VISION` sends page images to the model. The model must accept images. If yours does not, the
  bot says so in the report and keeps the local text; set `OCR_VISION_MODEL` to a model that does.
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

The tests run without a bot token, an API key or network access. The OCR tests render pages with
real damage (rotated, tilted, shadowed, blurred, noisy, low resolution, heavily compressed, two
columns, tables, scanned and mixed PDFs) and check what the pipeline reads from them. Tests for
Tesseract and EasyOCR run only where those engines are installed.

## Limits

- Handwriting, formulas and diagrams are read by the AI vision layer, not by local OCR. They need
  an AI model that accepts images (`OCR_VISION`, `OCR_VISION_MODEL`). Without one, formulas come out
  as plain characters, diagrams are skipped and messy handwriting may be misread or not read.
- EasyOCR, when enabled, reads English only. HEIC photos need `pip install pillow-heif` (not tested).
- A picture inside a page that also has a full text layer is not OCRed; that page's text layer is used.
- Material longer than `MAX_DOCUMENT_CHARS` is cut at that length, and the admin is told.
- Answers typed but not yet submitted are kept in memory and are lost if the bot restarts.
- The bot works in private chats only; it does not post to groups or channels.
