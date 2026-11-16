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
- **Storage:** SQLite (`data/bot.db`) with `users`, `assessments`, `questions` and `submissions` tables.
- **Providers:** Anthropic plus any OpenAI-compatible API (OpenAI, Gemini, Groq, OpenRouter,
  DeepSeek, Mistral, Ollama, custom). Configured entirely from `.env`.
- **Default provider:** Kilo AI Gateway with `kilo-auto/free`, so a Kilo API key in `AI_API_KEY` is the
  only AI setting needed.

### PR #2: OCR, photos and `/explain` (this PR)

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

### Limits and behaviour
- Private chats only; the bot does not post to groups or channels.
- Answers typed but not yet submitted are held in memory and lost on restart.
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
| Admin | `/generate` | Reply to a message, file or photo to use it as material |
| Admin | `/assessments`, `/results [id]`, `/close <id>`, `/regrade <id>`, `/users` | Manage and review |

## 5. Settings added or changed in this PR

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
- [ ] Back up `data/bot.db` if you already have data (the schema is unchanged by this PR).
