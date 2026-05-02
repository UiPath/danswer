# TESTING.md

Smoke tests for the analytics + retention + indexing pipelines, driven
by auto-generated data. Five pieces:

- `backend/scripts/seed_test_data.py` — pumps in tagged synthetic data.
- `backend/scripts/test_analytics_e2e.py` — end-to-end orchestrator for
  the analytics rollup + chat retention pipelines.
- `backend/scripts/test_features_e2e.py` — end-to-end orchestrator for
  the rest of this session's features (priority ordering, index_attempt
  retention, permission_sync_run terminal-only retention, resolved-button
  feedback DB write).
- `backend/scripts/test_celery_jobs_smoke.py` — fires both daily Celery
  tasks via `.delay()` against fresh dummy data and waits for the worker
  to complete them. Proves the broker → worker → DB pipeline is alive
  end-to-end (the same path beat uses for the 07:30 / 08:00 UTC daily
  fires). Run this anytime you want confidence the worker is processing
  tasks.
- This file — manual UI checklist + how to run each script.

> **Run only against a dev / staging DB.** Both scripts write *and* delete
> rows. They print the target DB URL and ask for a `yes` confirmation
> unless `--yes` is passed. Tagging via `__test_seed__` is what keeps
> `--clean` from touching real rows, but the rollup truncation in the
> orchestrator's Phase 2a / Phase 8 is unconditional — don't run it
> against prod.

---

## Quick start

```bash
cd backend

# 1. One-shot end-to-end test. Seeds, asserts, cleans up after itself.
PYTHONPATH=$(pwd) python scripts/test_analytics_e2e.py
# expected: every phase PASSES, exit 0.

# 2. If you want to poke around the seeded data manually, keep it after.
PYTHONPATH=$(pwd) python scripts/test_analytics_e2e.py --keep-data --yes

# 3. Tear it down later.
PYTHONPATH=$(pwd) python scripts/seed_test_data.py --clean --yes
```

The orchestrator's last line is either `✅ All phases passed.` or
`❌ N assertion(s) failed.` with PASS/FAIL details for every step.

```bash
# Companion suite for the non-analytics features (priority, index_attempt
# retention, permission_sync_run, resolved-feedback). Same shape, smaller
# scope. Self-contained — uses its own `__test_features__` tag prefix.
PYTHONPATH=$(pwd) python scripts/test_features_e2e.py --yes

# Live broker + worker plumbing check — fires both daily Celery tasks
# via `.delay()`, waits for the worker to apply side effects, asserts.
# Uses its own `__test_celery__` tag prefix. ~10 seconds when the worker
# is healthy. If `.get()` hangs, the worker is dead.
PYTHONPATH=$(pwd) python scripts/test_celery_jobs_smoke.py --yes
```

---

## What the analytics orchestrator covers

Each phase is documented with what it asserts in
`scripts/test_analytics_e2e.py`. In summary:

| Phase | What it does | What it asserts |
|---|---|---|
| 1 | Clean + seed 60 days of chats + a small batch of 35–90d "old" chats + search_doc links + slack configs | Sessions / messages / old chats / search_docs all present |
| 2a | Truncate `analytics_daily_rollup` + delete the checkpoint row | (housekeeping) |
| 2b | Run `backfill_analytics_rollup.py` | Rollup table populated, checkpoint row written |
| 3 | Call `fetch_*_from_rollup` directly | ≥60 rows per series, NPS-strict computable, sums look sane |
| 4 | `cleanup_stale_db.py --dry-run --policy=chat` | Dry-run completes without error |
| 5 | `cleanup_stale_db.py --policy=chat` (real run) | Old chats gone, fresh chats untouched, orphan search_docs cleaned |
| 6 | Re-query rollup | Still ≥60 days of data — proves rollup survived retention |
| 7 | Re-run `run_rollup` | Idempotent, checkpoint advances to today |
| 8 | Final cleanup of seeded rows + rollup table + checkpoint | (cleanup) |

If any phase fails, the script halts and exits non-zero. Re-running is
safe.

## What the features orchestrator covers

| Phase | What it does | What it asserts |
|---|---|---|
| 1 | Seed 5 NOT_STARTED `index_attempt` rows with priorities `[0, 5, 10, 0, 3]` and call `get_not_started_index_attempts` | Order is `[10, 5, 3, 0, 0]` (priority DESC), tiebreak is `time_created` ASC, `update_index_attempt_priority` clamps to ceiling=100 and refuses on IN_PROGRESS rows |
| 2 | Seed 25 SUCCESS `index_attempt` rows 70-94d old, run retention with `RETENTION_DAYS_INDEX_ATTEMPT=60` and `KEEP_LAST_N=20` | 5 oldest deleted, 20 newest kept. Acts as a regression check on the status-casing — the column stores enum NAMES (uppercase 'SUCCESS' / 'FAILED'), so the SQL must filter uppercase. A lowercase regression silently no-ops the policy |
| 3 | Seed 8 `permission_sync_run` rows (5 terminal + 3 in_progress, all 90d old), run retention | 5 terminal rows deleted, 3 in_progress preserved. Verifies the safety contract: stuck syncs aren't swept up by retention regardless of age |
| 4 | Synthetic call to `create_chat_message_feedback` mirroring the resolved-button handler signature (`predefined_feedback='resolved'`, `is_positive=None`, `user_id=None` against a slackbot-style session) | Exactly one feedback row is written with the right shape; `chat_message_id` matches the seeded message |

Tagged with `__test_features__` (separate from `__test_seed__`) — the two
orchestrators don't interfere.

## What the Celery smoke test covers

| Step | What it does | Confidence gained |
|---|---|---|
| 1 | Seed 5 old chats (35-90d) + 5 fresh chats (≤6d), tagged `__test_celery__` | (setup) |
| 2 | Snapshot `analytics_daily_rollup` row count + `max(rolled_up_at)` + chat counts | Baseline for diff |
| 3 | `run_analytics_rollup_task.delay()` → `.get(timeout=120)` | Celery client → broker → worker → DB write path is alive for the rollup task body |
| 4 | `run_retention_policies_task.delay()` → `.get(timeout=300)` | Same pipeline alive for retention; worker can execute long(er) tasks |
| 5 | Re-snapshot, diff against before | `max(rolled_up_at)` advanced; 5 old chats deleted; 5 fresh chats untouched |

If `.get()` hangs (script never exits), the worker isn't picking up
tasks. Check `kubectl exec ... -- tail -f /var/log/celery_worker.log`
and supervisord status — the same `'TaskPool' has no attribute 'grow'`
or `Invalid value for '-A'` failures we've hit before.

---

## Seeding manually

For one-off experiments without the full orchestrator:

```bash
# Standard scenario: 60 days × 10 chats/day, mix of Slackbot + UI.
PYTHONPATH=$(pwd) python scripts/seed_test_data.py \
    --days=60 --chats-per-day=10 \
    --slackbot-share=0.7 \
    --feedback-rate=0.6 --like-share=0.5 --resolved-share=0.2 --needs-help-share=0.1 \
    --users=15 --connectors=4 --docs-per-connector=200 \
    --with-old-data --with-search-docs --yes

# Heavy load (~5k chats over 90 days)
PYTHONPATH=$(pwd) python scripts/seed_test_data.py --days=90 --chats-per-day=50 --yes

# All-positive feedback (NPS should be ~+100)
PYTHONPATH=$(pwd) python scripts/seed_test_data.py \
    --feedback-rate=1.0 --like-share=1.0 --resolved-share=0.0 --needs-help-share=0.0 \
    --yes

# All-negative (NPS ~-100)
PYTHONPATH=$(pwd) python scripts/seed_test_data.py \
    --feedback-rate=1.0 --like-share=0.0 --resolved-share=0.0 --needs-help-share=0.0 \
    --yes  # remainder maps to dislikes
```

After seeding, populate the rollup:

```bash
PYTHONPATH=$(pwd) python scripts/backfill_analytics_rollup.py --yes 2>/dev/null \
  || PYTHONPATH=$(pwd) python scripts/backfill_analytics_rollup.py
```

---

## Manual UI smoke checklist

Open `/admin/analytics` after seeding (and after a backfill). Verify
visually:

- [ ] Top KPI row shows non-zero numbers for **Total Queries**, **Peak
      Daily Active Users**, **Auto-Resolution Rate**, **NPS — strict**.
- [ ] Second KPI row shows **Total Docs Indexed**, **Slack Channels
      Enabled**, **Positive Feedback %**, **Sources Active**.
- [ ] **Users and Query Trend** chart renders with Queries + Active
      Users series, both non-zero across the date range.
- [ ] **Feedback Trend** chart renders Likes + Dislikes.
- [ ] **Docs Indexed by Source** BarList shows per-source totals,
      sorted DESC.
- [ ] Date range picker → **Last 7 days** → numbers shrink, charts
      contract to 7 buckets.
- [ ] Date range picker → **Last 90 days** → charts expand. (Requires
      seed `--days=90` or longer.)
- [ ] Granularity dropdown → **Monthly** → charts collapse to monthly
      buckets. Subtitles update ("Monthly (Active Users = peak day)").
      Sums roughly match daily totals × num_days.
- [ ] No console errors in browser devtools. No 404 / 500 in network
      tab.
- [ ] Empty range (e.g. far future date) → charts show
      "No data in this date range" instead of erroring.

---

## Manual Slack feedback verification

The seed script doesn't simulate Slack button clicks. To verify the
**resolved-button feedback recording** end-to-end, do this manually
once after deploying to a connected Slack workspace:

1. Have the bot answer a question in a channel it's configured for.
2. Click "I'm all set!" (the immediate-resolved button).
3. Check `chat_feedback`:
   ```sql
   SELECT id, chat_message_id, is_positive, predefined_feedback,
          required_followup, feedback_text
     FROM chat_feedback
     ORDER BY id DESC LIMIT 5;
   ```
   Latest row should have `predefined_feedback = 'resolved'` and the
   right `chat_message_id`.
4. In a separate thread, click "I need more help", then have someone
   click "Mark Resolved".
5. Two new feedback rows: one with `required_followup=true`, the next
   with `predefined_feedback='resolved'` (latest wins for analytics —
   the session counts as resolved).

---

## Cleanup

Always tag-scoped — won't touch real rows.

```bash
PYTHONPATH=$(pwd) python scripts/seed_test_data.py --clean --yes
```

Also useful to reset the rollup pipeline state for a fresh run:

```sql
TRUNCATE TABLE analytics_daily_rollup;
DELETE FROM key_value_store WHERE key = 'analytics_rollup_state';
```

(The orchestrator does both as part of Phase 8 unless `--keep-data`.)

---

## Troubleshooting

**"No persona found" / "No embedding_model found":** the seeder needs
the default persona + embedding model rows to satisfy NOT NULL FKs.
Bootstrap by running `alembic upgrade head` and starting the API server
once — it loads default personas and the initial embedding model on
startup. Then re-run the seeder.

**Phase 5 (real retention) failed with "old chats deleted":** check
your `RETENTION_DAYS_CHAT`. Default is 30. If you've changed it to
something larger than 35 days, the seeded "old" data (35–90 days old)
won't be eligible for deletion. Either lower `RETENTION_DAYS_CHAT` or
re-seed with older data:
```bash
# In seed_test_data.py::seed_old_chat_for_retention, the range is
# rng.randint(35, 90). Increase it if your retention is higher.
```

**Phase 7 (rollup idempotency) failed with "checkpoint advanced to
today":** UTC vs local time mismatch. The checkpoint stores ISO date
strings in UTC. Re-running near midnight UTC can flip the date between
phases. Re-run during the day; this is a benign artifact of the test,
not a real bug.

**`ANALYTICS_LATE_FEEDBACK_BUFFER_DAYS` exceeds `RETENTION_DAYS_CHAT - 2`:**
the rollup logs a warning and caps the window at the safe floor.
Expected — see the rollup module docstring. To suppress, lower
`ANALYTICS_LATE_FEEDBACK_BUFFER_DAYS`.

**Foreign-key violation on cleanup:** the `--clean` path deletes in
explicit FK order, but if you've manually inserted real data referencing
seeded rows (e.g. a real user added a feedback to a `__test_seed__`
chat), `--clean` will fail. Resolve the dangling reference manually,
then re-run `--clean`.

**`document_by_connector_credential_pair` row count mismatch:** docs
are linked per cc-pair via `document_by_connector_credential_pair`;
the seeder inserts both the `document` row and the join row. If a
prior partial run left orphans, `--clean` removes them — both tables
are scoped by the `__test_seed__` prefix.
