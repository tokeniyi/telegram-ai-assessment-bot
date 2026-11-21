# Roadmap

The planned order of work on the bot. Stages 1 and 2 are done. Stage 3 picks up what Stage 1 left
"for a later stage"; the stages after it are proposals and can be reordered.

Stages 3 and 4 finish the reliability work begun in Stages 1 and 2, so they come first.
Stages 5 to 7 are independent of each other.

| Stage | Topic | Status |
|---|---|---|
| 1 | Saved answers survive restarts | ✅ Done ([#8](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/8)) |
| 2 | Each Telegram update handled once | ✅ Done ([#10](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/10)) |
| 3 | Work that survives a restart | Next |
| 4 | Safer deploys | Planned |
| 5 | Visibility for the admin | Proposed |
| 6 | Assessment features | Proposed |
| 7 | Scale and limits | Proposed |

---

## Done

### Stage 1: Saved answers survive restarts ([#8](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/8))

Answers typed so far, the assessment picked with `/pending` and chat history are stored in
PostgreSQL (`user_sessions`, `draft_answers`) instead of the bot's memory, so a restart or redeploy
loses nothing. Buttons sent by the previous process keep working. Submit creates at most one
submission, even on a double tap or with two bot versions running.

Left for later: grading and sending still run only in memory (see Stage 3).

### Stage 2: Each Telegram update handled once ([#10](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/10))

Telegram resends updates the bot did not confirm (after a crash, or while two versions poll during a
deploy). The first handler now claims each update's ID in `processed_updates`; copies are dropped
before any other handler runs, in any process.

- At most once: an update cut short by a crash is not replayed; the user sends it again.
- Fails open: if the ID cannot be recorded, the update is still handled.
- Records older than 48 hours are pruned.

Not covered: two taps are two different updates, so they are not merged here (see Stage 3).

---

## Next

### Stage 3: Work that survives a restart

Today, slow work runs inside the handler that started it. A restart or redeploy in the middle loses
it, and nothing picks it up again.

| Problem | What happens today | Planned fix |
|---|---|---|
| Sending an assessment out | Students receive it one by one (`_broadcast` in `bot/handlers/admin.py`). A restart halfway through leaves the rest without it, and nothing resumes. | Store one "to send" row per student when the admin publishes, and mark each row sent. On start, the bot finishes any unsent rows. |
| Grading | A restart mid-grading leaves the submission `pending` until an admin runs `/regrade`. | On start, regrade stale `pending` submissions automatically, with a retry limit, then send the student the result. |
| Two quick taps on "Send to all users" | `on_draft_button` checks the status and then sets it in a separate step. Two taps both pass the check, so everyone receives the assessment twice. Stage 2 does not catch this: each tap is a different update. | Change the status in one database step (`UPDATE … WHERE status = 'draft' RETURNING`), so only one tap goes through. |

The double-tap fix is small and can ship on its own first.

Done when: a restart during sending or grading needs no admin action, and no student receives an
assessment twice.

### Stage 4: Safer deploys

Most duplicates come from the window when two bot versions run at once during a Railway deploy.
This stage makes that window short or removes it.

- **Health check.** Railway switches over only once the new version is ready.
- **Clean shutdown.** On stop, the bot stops taking new updates and finishes the work in progress.
- **Optional: webhook instead of polling.** Telegram then sends each update to one address, so two
  versions never fetch the same updates.

Done when: a redeploy during normal use produces no `already handled` log lines and no
interrupted work.

---

## Proposed

### Stage 5: Visibility for the admin

Problems currently show up only in the server logs, or when a student complains.

- An admin command (for example `/status`) showing grading still waiting, failed grading, failed
  sends and whether the AI provider is reachable.
- An alert to the admins when grading keeps failing or the AI provider is down.
- Clearer, structured logs for sending, grading and OCR.

### Stage 6: Assessment features

Product work, safer to build once Stages 3 to 5 make sending and grading reliable.

- Deadlines, with assessments closing automatically.
- Reminders to students who have not submitted before the deadline.
- Per-assessment settings for whether students see reference answers and whether they can
  resubmit. Both are one setting for the whole bot today (`SHOW_CORRECT_ANSWERS`,
  `ALLOW_RESUBMISSION`).

### Stage 7: Scale and limits

Protects the free AI tier and the server's memory as the number of users grows.

- Per-user rate limits on AI calls (chat, `/explain`, grading).
- A cap on how many OCR jobs run at once across users.
- A sending speed that respects Telegram's limits for large classes.
