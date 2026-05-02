---
description: Run the project's automated checks (TypeScript, mypy, pytest if scoped) and summarise failures.
allowed-tools: Bash, Read
---

Run the standard validation suite for whatever scope the user gave (default: both backend + frontend). Output a tight summary with pass/fail counts and the first error per failing tool — don't paste full logs back unless the user asks.

# Steps

1. **Decide scope from the user's message.**
   - If they wrote `/runtests backend` → only backend checks.
   - If `/runtests web` or `/runtests frontend` → only frontend checks.
   - If `/runtests <path>` → only checks scoped to that path (mypy on Python files, tsc on TS files, pytest on tests dir).
   - Otherwise → run both backend and frontend.

2. **Backend checks (run sequentially, stop reporting on each):**
   - `cd backend && python -m mypy . 2>&1 | tail -50` — mypy across the whole backend.
   - `cd backend && pytest -x --tb=short 2>&1 | tail -50` — fast-fail pytest. Most modules don't have tests, so a "no tests collected" result is normal; that's not a failure.
   - `cd backend && alembic heads 2>&1` — confirm single head (multiple heads means a migration was added on a parallel branch and needs merging).

3. **Frontend checks:**
   - `cd web && npx tsc --noEmit --incremental 2>&1 | tail -40` — TypeScript across `web/src/`.
   - `cd web && npm run lint 2>&1 | tail -40` — ESLint.
   - `cd web && npx prettier --check "src/**/*.{ts,tsx,js,jsx}" 2>&1 | tail -20` — prettier formatting check.

4. **Skip what's not relevant.** Don't run frontend checks for backend-only changes and vice versa. If you can infer from `git diff --stat` that only `backend/` was touched, skip frontend.

5. **Summarise**, format like:

   ```
   Backend
     mypy        ✓ clean   (or ✗ N errors — first: <file:line — message>)
     pytest      ✓ N passed (or ✗ N failed)
     alembic     ✓ single head
   Frontend
     tsc         ✓ clean   (or ✗ N errors — first: <file:line — message>)
     lint        ✓ clean
     prettier    ✓ clean
   ```

6. **If anything failed**, follow up with the user: "X failures above. Want me to fix them, or are you debugging a specific area?" Don't auto-fix unless explicitly told to.

# Notes

- Everything in this file's command list is in `.claude/settings.json`'s allow-list, so none of it should prompt for permission.
- `pytest` runs against the live local stack (Postgres / Vespa / model server). If they're not running, integration-style tests will fail — flag that as "stack not running" rather than as test failures.
- Don't run `npx playwright test` from this command. E2E is too slow for an inner-loop check; use it on demand only.
- Don't run `pre-commit run --all-files` from here either — that's slow and overlaps with the per-tool checks above. The user can `pre-commit run` themselves before commit.
