# Bot message and user-flow audit

Source: `main` before `feat/user-friendly-bot-messages`. This inventory identifies user-facing messages and their triggering paths. Dynamic fields (first names, assessment IDs, counts, AI output, scores) vary by conversation.

## Entry and help
- `/start` (student): `👋 Hi {first_name}! You are registered and will receive assessments here.` followed by `USER_HELP` in `bot/handlers/common.py`.
- `/start` (admin): `👋 Hi {first_name}! You are registered as an administrator.` followed by `USER_HELP` and `ADMIN_HELP`.
- `/help`: `USER_HELP` (plus `ADMIN_HELP` for admins).
- `/myid`: `Your Telegram ID is {id}` plus role/configuration note.

## Student flow
- Assessment published: `📝 {title}`, question count and points; questions; answer instructions (`student.assessment_blocks`).
- `/pending` no items: `Nothing is waiting for your answers. 🎉`
- `/pending` with items: `Open assessments. Tap one to see its questions and answer it:` and buttons.
- Assessment selection when unavailable: `That assessment is closed or you have already submitted it.`
- Answers: `Saved as your answer to {title} ({n} messages so far)` and Submit / Undo / Discard.
- No assessment: `There is no assessment waiting for your answers right now.`
- Draft size exceeded: `Your answers are too long. Submit what you have, or /cancel and start again.`
- Undo / discard / submit: callback notices, progress, unavailable/expired assessment notices.
- Grading result: `📊 Result — {title}`, score, per-question breakdown and feedback.
- Grading failed: answers saved; grading retried by administrator; administrator gets a `/regrade` notice.
- `/myresults`: submitted scores or `You have not submitted anything yet.`
- `/cancel`: `Your typed answers were discarded.` or `There was nothing to discard.`

## Admin flow
- Access denied: `This command is for administrators only.`
- Upload or `/generate`: usage instructions or material-length warning, `⏳ Generating questions with {model}…`, error or draft preview.
- Draft preview: `📝 Draft #{id} — {title}`, question count/points, reference answers, `Send to all users` / `Regenerate` / `Discard` buttons.
- Publish: number of recipients, blocked/failed recipients, `/results {id}` hint.
- `/assessments`: newest-first status list or no-assessments notice.
- `/results [id]`: status and submissions, scores, not-graded warning, CSV attachment.
- `/close <id>`: missing-ID instructions, invalid-status notice or closed confirmation.
- `/regrade <id>`: already-graded notice, progress or success/failure notice.
- `/users`: registered/reachable counts.

## Shared assistance, chat, OCR and errors
- `/explain`: usage guide, disabled notice, processing-in-progress note, explanation, failure notice.
- Free-form chat: AI answer, saved-answer confirmation, offer-to-submit prompt, or AI-outage notice.
- Photos/files: reading/OCR progress, extraction summary and warnings, size/download errors, text echo for handwritten responses.
- Unexpected exceptions: generic `Something went wrong on my side. Please try again.` message.

## UX refinement scope
- Shorten onboarding; show the most relevant role's instructions first.
- Keep slash commands, callback data, and role permissions unchanged.
- Make assessment discovery, answer submission, administrative review, and retry instructions clearer.
- Preserve existing test-sensitive messages and behavior where possible.
- Avoid changing AI prompts, grading logic, persistence, security, or deployment configuration.

## Testing
- Manual: run `/start` and `/help` as both a student and an admin, and check Telegram HTML formatting.
- Student: publish an assessment; check `/pending`, upload, Undo, Discard, Submit, grading failure and results.
- Admin: upload material; regenerate/discard/publish; check `/assessments`, `/results`, `/close`, `/regrade`, `/users`.
- Run `pytest` on the branch before merging. No live Telegram test or CI execution is claimed here.
