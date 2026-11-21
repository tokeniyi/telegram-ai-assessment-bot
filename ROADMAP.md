# Roadmap

The planned order of work on the bot. Stages 1 to 4 are done and Stage 5 is in review. The stages
after it are proposals and can be reordered. What was built in each stage, and how it was tested,
is in [docs/implementations](docs/implementations/README.md).

Stages 3 and 4 finish the reliability work begun in Stages 1 and 2, so they come first.
Stages 5 to 7 are independent of each other.

| Stage | Topic | Status |
|---|---|---|
| 1 | Saved answers survive restarts | ✅ Done ([#8](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/8)) |
| 2 | Each Telegram update handled once | ✅ Done ([#10](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/10)) |
| 3 | Work that survives a restart | ✅ Done ([#12](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/12)) |
| 4 | Safer deploys | ✅ Done ([#13](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/13)) |
| 5 | Visibility for the admin | 🔍 In review ([#14](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/14), [phase 05 docs](docs/implementations/phase-05/summary.md)) |
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

### Stage 4: Safer deploys ([#13](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/13))

`GET /health` on `PORT` answers 200 only while updates are fetched and the database answers, so
Railway switches over once the new version is ready. On SIGTERM the bot stops fetching, confirms
fetched updates to Telegram, finishes the updates and the background item in hand within
`SHUTDOWN_GRACE_SECONDS`, then exits. An optional webhook (`WEBHOOK_URL`) removes double polling.
Details: [phase 04](docs/implementations/phase-04/summary.md).

Left for later: the Railway settings (healthcheck path, draining time) are made in the dashboard,
and no real redeploy was verified from the development environment.

---

## Next

### Stage 5: Visibility for the admin

Problems used to show up only in the server logs, or when a student complained.

- `/status` for admins: grading waiting, interrupted and failed, results not delivered,
  assessment messages queued or failed, and a live check of the AI provider (no tokens used).
- Alerts to the admins when the AI provider fails 3 requests in a row and when it is back;
  grading failures reported at once, then batched.
- Structured log events for sending, grading, OCR and AI requests; `LOG_FORMAT=json` for Railway.

Details: [phase 05](docs/implementations/phase-05/summary.md).

---

## Proposed

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
