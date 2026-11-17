# CI/CD deployment smoke test

This document is a documentation-only change used to verify the GitHub Actions → Railway continuous deployment path. It intentionally does not modify application code, dependencies, Docker configuration, or environment variables.

## Verification checklist

1. Open this branch's pull request targeting `main`.
2. Confirm the GitHub Actions `tests` workflow runs and passes.
3. Merge the PR after required checks and reviews pass.
4. Confirm Railway detects the new `main` commit and builds the Docker image.
5. Confirm the new deployment becomes active, with the bot startup log and no database connection errors.
6. Test `/start` and `/pending` from Telegram, then verify OCR/assessment workflows separately.
7. Compare the deployment commit SHA against the merge commit SHA; note that squash/merge commit IDs may differ from the PR head.

**Scope:** This checks the deployment pipeline; it does not establish zero downtime or guarantee in-flight OCR/AI requests survive redeployment. The polling bot must not run multiple competing pollers with the same token.
