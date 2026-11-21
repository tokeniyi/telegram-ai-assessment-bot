# Roadmap

The planned order of work on the bot. Stages 1 to 3 are done and Stage 4 is in progress. The stages
after it are proposals and can be reordered. What was built in each stage, and how it was tested,
is in [docs/implementations](docs/implementations/README.md).

Stages 3 and 4 finish the reliability work begun in Stages 1 and 2, so they come first.
Stages 5 to 7 are independent of each other.

| Stage | Topic | Status |
|---|---|---|
| 1 | Saved answers survive restarts | ✅ Done ([#8](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/8)) |
| 2 | Each Telegram update handled once | ✅ Done ([#10](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/10)) |
| 3 | Work that survives a restart | ✅ Done ([#12](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/12)) |
| 4 | Safer deploys | 🔧 In progress ([phase 04 docs](docs/implementations/phase-04/summary.md)) |
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

### Stage 3: Work that survives a restart ([#12](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/12))

Sending an assessment and grading no longer run only inside the handler that started them.
Publishing writes one `deliveries` row per student in the same transaction as the status change
(`UPDATE … WHERE status = 'draft' RETURNING`), so a double tap publishes once. Each message and
each grading run is leased to one process; on start and every 30 seconds the bot finishes what is
left, with retry limits. Details: [phase 03](docs/implementations/phase-03/summary.md).

Left for later: a process killed between Telegram accepting a message and the row being marked
sent sends it again after the lease (see Stage 4).

---

## Next

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
