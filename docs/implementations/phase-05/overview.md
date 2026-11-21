# Phase 05 — Visibility for the admin: Phase overview

| | |
|---|---|
| Roadmap stage | Stage 5 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/lucid-brahmagupta-dri4yd` |
| Pull request | not opened yet |
| Status | In progress |

## Objective

Problems with sending, grading or the AI provider show up only in the server logs, or when a
student complains. This phase lets the admins see the state of the bot from Telegram, tells them
when something keeps going wrong, and makes the logs readable and searchable.

## Scope

In scope (the three items of Stage 5 in the roadmap):

- **`/status`** (admins only): grading waiting, in progress, interrupted and failed; results not
  delivered; assessment messages queued, failed and blocked; whether the AI provider answers now
  (a live check that uses no tokens) and how the last AI requests went; open alerts.
- **Alerts to the admins:** when the AI provider fails several requests in a row, and when it
  answers again; grading failures, batched so that an outage does not send one message per
  student.
- **Structured logs** for sending, grading, OCR and AI requests: one event name and `key=value`
  fields per line, or JSON lines (`LOG_FORMAT=json`) that Railway turns into searchable
  attributes.

Out of scope: retrying failed grading automatically once the AI is back (admins still run
`/regrade`; see [summary.md](summary.md)); a command to resend failed assessment messages;
per-user rate limits (Stage 7); deadlines and reminders (Stage 6).

## Expected outcome

From the roadmap: an admin sees grading still waiting, failed grading, failed sends and whether
the AI provider is reachable with one command; admins are alerted when grading keeps failing or
the AI provider is down; sending, grading and OCR write clear, structured log lines.

## Builds on

- Phase 03 outstanding issue: "Failed sends are only visible in the log and the publisher's
  report. An admin view of failed deliveries and stuck grading belongs to Stage 5 (`/status`)."
  `/status` reads the `deliveries` rows and the grading leases (`grading_claimed_until`,
  `grading_attempts`) that Phase 03 introduced.
- Phase 03 decision: exactly-once decisions live in PostgreSQL. Each alert is sent once across all
  bot processes because a conditional update on a shared row decides who sends it, not memory.
- Phase 04: two versions run at once during a deploy. New columns are added in place and an older
  version ignores them; its own per-failure notices keep working while both run.
- Phase 04 decision: `bot/runtime.py` owns the lifecycle. Logging is set up in `main.py` before it.

## Facts this phase relies on

From Railway's documentation, read through web-search results on 2026-11-21 (docs.railway.com is
not reachable from the development environment): Railway parses a log line that is one JSON
object, uses its `level` and `message` fields, and makes every other field a filterable attribute
(`@name:value`); plain lines written to stderr are shown as errors. Whether a JSON line on stderr
keeps its own `level`: **Unable to verify**, so the bot now logs to stdout.
