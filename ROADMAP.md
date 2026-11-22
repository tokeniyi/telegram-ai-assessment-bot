# Roadmap

The planned order of work on the bot. Stages 1 to 7 are done and Stage 8 is in review. The
stages after them are proposals and can be reordered. What was built in each stage, and how it was tested,
is in [docs/implementations](docs/implementations/README.md).

Stages 3 and 4 finish the reliability work begun in Stages 1 and 2, so they come first.
Stages 5 to 9 are independent of each other. Stage 6 was added on 2026-11-22, at the user's request;
the proposed stages after it were renumbered (Assessment features 6 → 7, Scale and limits 7 → 8).
Stage 8 was added the same day, also at the user's request, from the outstanding issues of Phases
05 and 06; Scale and limits became Stage 9.

| Stage | Topic | Status |
|---|---|---|
| 1 | Saved answers survive restarts | ✅ Done ([#8](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/8)) |
| 2 | Each Telegram update handled once | ✅ Done ([#10](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/10)) |
| 3 | Work that survives a restart | ✅ Done ([#12](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/12)) |
| 4 | Safer deploys | ✅ Done ([#13](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/13)) |
| 5 | Visibility for the admin | ✅ Done ([#14](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/14), [phase 05 docs](docs/implementations/phase-05/summary.md)) |
| 6 | Command guidance | ✅ Done ([#15](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/15), [phase 06 docs](docs/implementations/phase-06/summary.md)) |
| 7 | Assessment features | ✅ Done ([#16](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/16), [phase 07 docs](docs/implementations/phase-07/summary.md)) |
| 8 | Recovery without the admin | 🔍 In review ([#17](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/17), [phase 08 docs](docs/implementations/phase-08/summary.md)) |
| 9 | Scale and limits | Proposed |

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

### Stage 5: Visibility for the admin ([#14](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/14))

Problems used to show up only in the server logs, or when a student complained.

- `/status` for admins: grading waiting, interrupted and failed, results not delivered,
  assessment messages queued or failed, and a live check of the AI provider (no tokens used).
- Alerts to the admins when the AI provider fails 3 requests in a row and when it is back;
  grading failures reported at once, then batched.
- Structured log events for sending, grading, OCR and AI requests; `LOG_FORMAT=json` for Railway.

Details: [phase 05](docs/implementations/phase-05/summary.md).

### Stage 6: Command guidance ([#15](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/15))

Users should not have to remember commands. When something fails, the bot says what went wrong in
plain words and what to do next; when it succeeds, it suggests the next step only if there is one.

- One command registry (`bot/commands.py`) for the menus, `/help`, "did you mean" and every
  suggestion; tested against the handlers that are really registered.
- Problems are told apart: wrong input, no permission, not possible in this state, a temporary
  outage, or already being retried (no action needed).
- Suggested commands carry real ids from the database, as tap-to-copy code, and as buttons for
  safe commands. `/regrade` is suggested only for failed grading.

Details: [phase 06](docs/implementations/phase-06/summary.md).

### Stage 7: Assessment features ([#16](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/16))

Product work, built on the reliable sending and grading of Stages 3 to 5.

- `/deadline <id> <when>` (`2d`, `tomorrow 17:00`, `2026-11-30 17:00` in `TIMEZONE`, `off`). Answers
  stop at the deadline itself; the assessment is then closed and its publisher told once.
- Students who were sent it and have not submitted are reminded `REMINDER_HOURS` (24) before, once
  each, with a button to open the questions or submit what they saved.
- `/settings <id>` (also a button under the draft preview): whether students of this assessment see
  reference answers and may resubmit. `SHOW_CORRECT_ANSWERS` and `ALLOW_RESUBMISSION` become the
  defaults.

Details: [phase 07](docs/implementations/phase-07/summary.md).

---

## Next

### Stage 8: Recovery without the admin ([#17](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/17))

Two gaps left by Phases 05 and 06, where something failed and nothing happened until an admin acted.

- `/resend <id>`: send an open assessment again to the users whose message failed (timed out, sent
  in part, or failed 3 times). Suggested by the publish report and `/status`; typed, never a button,
  because a student whose send timed out may get a second copy.
- Failed grading is retried by the bot itself once the AI provider answers again, up to
  `AUTO_REGRADE_ATTEMPTS` (3) times per submission, 5 minutes after the failure and doubling. The
  student gets the result; the admins get one batched "retried automatically" message. `/regrade`
  stays for manual retries.

Details: [phase 08](docs/implementations/phase-08/summary.md).

---

## Proposed

### Stage 9: Scale and limits

Protects the free AI tier and the server's memory as the number of users grows.

- Per-user rate limits on AI calls (chat, `/explain`, grading).
- A cap on how many OCR jobs run at once across users.
- A sending speed that respects Telegram's limits for large classes.

---

## Outside the stages

Side features the user asked for outside the staged plan. They are documented like phases, in
[docs/implementations](docs/implementations/README.md), but have no stage number, and the stages
above do not depend on them.

| Feature | Status | Docs |
|---|---|---|
| Study check-ins | In progress (not opened yet) | [feature-study-checkins](docs/implementations/feature-study-checkins/summary.md) |

### Study check-ins

Added on 2026-11-22 at the user's request. The bot writes to students between assessments, not only
about them.

- Students are offered check-ins once, at `/start`, and choose with `/checkins on|off|pause`.
- A student who says yes gets a short AI-written message every `CHECKIN_DAYS` (3) days at
  `CHECKIN_HOUR`. It suggests something to review: an assessment still open, a question they lost
  points on, or something they asked. It also invites them to reply if they are stuck.
- If the AI is down, a built-in message is sent instead.
- Each check-in is sent at most once, even with two processes. `/status` shows how they went.

Later: once study-session memory exists, it becomes one more source of facts for the AI.
