# CLAUDE.md

This file is the entry point for Claude (and other LLM coding agents) when
working in this repository. To keep one source of truth, the substantive
operating notes live in [`AGENTS.md`](./AGENTS.md). **Read that file
first.**

The rest of this document is a thin per-agent overlay: things that are
specifically useful for Claude / Claude Code without cluttering the
shared `AGENTS.md`.

---

## Critical pre-flight

**Before applying anything you've seen in upstream Onyx
(`github.com/onyx-dot-app/onyx`), check the "Divergence from upstream
Onyx" section in `AGENTS.md`.** This fork is roughly two years behind
upstream. Several upstream rules — `OnyxError` everywhere, no
`response_model=`, Celery-based indexing, `@shared_task`, `LLMFlow`
tracing, multi-tenant migrations — **do not apply here** and will break
things if introduced as drive-by changes.

When in doubt, `grep` for the construct in `backend/danswer/` first. If
it's missing, don't introduce it without asking the human.

---

## What lives where

- **Architecture, footguns, conventions, recipes**: [`AGENTS.md`](./AGENTS.md)
- **How to set up locally** (env vars, services, dev aliases):
  [`CONTRIBUTING.md`](./CONTRIBUTING.md)
- **Repo / monorepo layout** with annotations: AGENTS.md → "Top-level
  layout"
- **Common workflows** (add a connector, add a SQL field, edit
  credentials, etc.): AGENTS.md → "Common workflows"

---

## Claude-specific working norms

These are tuned for how Claude Code (and similar agents) tend to behave.
They don't apply only to Claude, but they're written with that workflow
in mind.

### Don't trust scrollback

The conversation context can be hundreds of messages by the time you
arrive. Skim `git log -10`, `git status`, and `git diff --stat` before
making any non-trivial change so you're operating on the actual current
state, not a remembered state. The user has been editing files between
your turns; assume the file on disk is canonical, not your memory of it.

### Prefer minimal, isolated edits

Most tasks here have come in small, focused changes (often single-file
or single-feature). Don't bundle unrelated cleanups into a feature edit
unless explicitly asked. The repo has long-standing inconsistencies
(camelCase / snake_case mismatches, deprecated `datetime` calls, copied
filter bugs in connector pages) — point them out as observations rather
than silently "fixing" them.

### Match the pattern, not the upstream

When adding new connectors or admin pages, the right template is the
**most-recently-edited equivalent in this repo**, not the upstream Onyx
version. Examples:

- New connector? Clone `connectors/github_files/connector.py`'s shape.
- New per-source-type admin page? Clone `web/src/app/admin/connectors/sf-account/page.tsx`.
- New backend bulk-fetch helper? Mirror `db/tasks.py::get_latest_tasks_by_names`.

Upstream's version of any of these is likely better-architected but is
also unrecognizable to this fork.

### Process bounce list

Several long-running services don't auto-reload on certain kinds of
changes. After any of these edits, ask the human to restart the relevant
process (you can't bounce them from inside the agent loop):

| Edit type | Restart |
|---|---|
| Add / remove a `DocumentSource` enum value | API server (`dapi`), background jobs (`dbe`), Slack listener (`dsl`). The Slack one is the easiest to forget — historically users saw `pydantic ValidationError: source_type` until they bounced it. |
| Modify a connector's `__init__` signature, credential schema, or factory mapping | Background jobs (`dbe`) — both indexer and Celery worker spawn from there. |
| Modify a SQLAlchemy model | Run `alembic upgrade head` from `backend/` first. Then bounce API server + background jobs. |
| Edit `web/src/lib/sources.ts`, `lib/types.ts`, or any TypeScript-side enum | Most edits hot-reload via `npm run dev`. If a new tile / source / route doesn't appear after a hard refresh, `rm -rf web/.next` and restart `dfe` — `.next/cache` occasionally holds stale module bundles. |

### When you write a plan

If the user asks for a plan or you're about to spawn a complex multi-step
change, use the four-section template borrowed from upstream's CLAUDE.md
(it's a good shape):

1. **Issues to address** — what the change is meant to do.
2. **Important notes** — non-obvious things you found while researching
   (e.g. "this endpoint also feeds the per-source pages, so removing
   field X would break them").
3. **Implementation strategy** — high-level. File names, function names,
   no code.
4. **Tests** — what you'll write to verify. The fork has scant test
   coverage; if you can't add a test, say so explicitly so the human can
   spot-check manually.

Skip "timeline" and "rollback plan" — they don't fit the development
shape here.

### When to pause

`AGENTS.md` has a "When to ask the human" list. The non-obvious additions
for Claude specifically:

- **Anything involving `dev_run_background_jobs.py` subprocess plumbing.**
  Past breakage was silent (worker died, no logs) and ate days. Have the
  human confirm the worker boots cleanly after any change there.
- **Any change to `update.py`'s scheduler / Dask submission.** Same
  reason — failure modes are subtle.
- **Renames that affect URLs.** `displayName` and the route folder are
  coupled by `lib/sources.ts::fillSourceMetadata`; a rename without
  matching updates is a 404 and breaks bookmarks.

---

## A note on instruction conflicts

If the conversation contradicts `AGENTS.md`/`CONTRIBUTING.md`, ask which
takes precedence — usually the user's immediate instruction wins, but
sometimes they're testing whether you remember the project rules. If the
user says "do X anyway" after you've flagged the conflict, do X and note
that you're overriding documented guidance.

If `AGENTS.md` and this file disagree, this file (CLAUDE.md) doesn't
override `AGENTS.md` — it's an overlay. Conflicts mean someone's drifted;
flag it.
