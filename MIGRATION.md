# Migration Guide

This branch combines three independent slices of work:

1. **Background indexing scaling** — Dask scheduler topology, split out
   into separate k8s deployments
2. **Redis caching + rate limiting** — read-through KV cache, per-user
   request rate limiter, persona-list cache with write-through
   invalidation
3. **Assistants UX rework** — Manage Assistants + Assistant Gallery
   pages, seed script for local UX testing

> **TL;DR — everything new is default OFF.** Deploying this branch
> as-is does **not** change runtime behaviour for the chat path or the
> background workers. You opt in per feature by setting env vars.

The only mandatory deltas at deploy time are:
- Two new Python deps (`redis`, `bokeh`) installed automatically when
  the backend image rebuilds against the new `requirements/default.txt`.
- A few non-secret env vars added to the configmap (all defaulting to
  empty/false — safe).

Everything else (Redis pod, cache enablement, rate limits, new
background topology) is opt-in.

---

## 1. What's in this branch — quick map

### Backend / infra

| Slice | Files | Default state |
|---|---|---|
| Redis foundation + KV cache | `backend/danswer/redis/redis_pool.py`, `dynamic_configs/store.py`, `factory.py`, `configs/app_configs.py` | `REDIS_KV_CACHE_ENABLED=""` → OFF |
| Per-user request rate limiter | `backend/danswer/server/middleware/request_rate_limit.py`, wired on `/send-message` + `/stream-answer-with-quote` | `REQUEST_RATE_LIMIT_ENABLED=""` → OFF |
| Persona list cache | `backend/danswer/db/persona_cache.py`, `db/persona.py` (write-through invalidation), `ee/danswer/db/user_group.py` | `PERSONA_CACHE_ENABLED=""` → OFF |
| Dask scheduler topology | `backend/danswer/background/update.py`, new `deployment/kubernetes/*` manifests | Existing single `background` pod still runs; new manifests not applied unless you `kubectl apply` them |

### Frontend / UX

| Page | What changed | Risk |
|---|---|---|
| `/assistants/mine` (Manage) | Drag-and-drop reorder, default pin, visibility toggle, search, bulk actions, undo toast | Cosmetic — backend unchanged |
| `/assistants/gallery` (Browse) | Sections, filter chips, sort, column picker (persists in localStorage), doc-set names | Cosmetic — backend unchanged |

### Tooling

| Item | Purpose |
|---|---|
| `backend/scripts/seed_assistants.py` | Local dev seed of ~50 assistants for UX testing |
| `REDIS_CACHING_PLAN.md` | Design rationale; not load-bearing for deploy |

---

## 2. New Python dependencies

`backend/requirements/default.txt` adds two pins:

```
redis==5.0.8                    # Redis client for the caching/rate-limit layer
bokeh>=2.4.2,<3.0               # Dask scheduler dashboard at :8787 (bg-scaling commit)
```

**Action**: rebuild the backend image. If you `pip install -r` in a
venv, also re-run that.

---

## 3. New env vars (env-configmap)

All default to empty/false. Add to `darwin-kubernetes/env-configmap.yaml`
(already done on this branch — verify and apply):

```yaml
# Redis connection (only used when one of the *_ENABLED flags below is true)
REDIS_HOST: "redis"               # in-cluster service name
REDIS_PORT: "6379"
REDIS_DB_NUMBER: "0"
REDIS_SSL: ""

# Feature flags — default OFF
REDIS_KV_CACHE_ENABLED: ""        # set to "true" to enable read-through KV cache
REDIS_KV_CACHE_TTL_SECONDS: "86400"
REQUEST_RATE_LIMIT_ENABLED: ""    # set to "true" to enable
REQUEST_RATE_LIMIT_PER_MINUTE: "" # set a number (e.g. "20") to cap; 0/empty = no per-min cap
REQUEST_RATE_LIMIT_PER_HOUR: ""   # set a number (e.g. "300") to cap; 0/empty = no per-hour cap
```

`PERSONA_CACHE_ENABLED` / `PERSONA_CACHE_TTL_SECONDS` live in
`backend/danswer/configs/app_configs.py` defaults — you can override
via env if you want to enable, but they're not in the configmap by
default. Add a line if you plan to enable.

---

## 4. New secrets

`darwin-kubernetes/secrets.yaml` gains one optional key:

```yaml
stringData:
  redis_password: ""              # empty for unauth'd in-cluster Redis
```

The `api_server` and `background` deployments reference it with
`optional: true`, so an absent or empty value is fine for the unauth'd
in-cluster Redis StatefulSet.

---

## 5. New Kubernetes manifests

### 5a. Redis StatefulSet (always needed if any Redis flag is on)

```bash
kubectl apply -f darwin-kubernetes/redis-statefulset.yaml
```

What it ships: a single-replica Redis 7.2-alpine, cache-only config
(no AOF, no RDB snapshots, `maxmemory 256mb`, `allkeys-lru`), exposed
as the `redis` ClusterIP Service on 6379. Pod restart drops the cache
— that's intentional; the source of truth is Postgres, and counters
self-heal as windows expire.

### 5b. Dask scaling topology (opt-in, Darwin manifests ready)

The bg-scaling commit added 5 upstream-style manifests under
`deployment/kubernetes/` (legacy / reference tree; **not** what
Darwin applies from — see AGENTS.md "Critical fact §9"). A later
commit on this branch ported each one to Darwin conventions under
`darwin-kubernetes/`, with the right image registry, configmap /
secrets wiring, REDIS_PASSWORD (optional), indexcpu node affinity,
darwin/indexing toleration, and PVCs:

- `darwin-kubernetes/background-beat-deployment.yaml`
- `darwin-kubernetes/background-celery-deployment.yaml`
- `darwin-kubernetes/background-indexer-scheduler-deployment.yaml`
- `darwin-kubernetes/dask-scheduler-service-deployment.yaml`
- `darwin-kubernetes/dask-worker-deployment.yaml`

Plus `deployment/docker_compose/docker-compose.dask-distributed.yml`
(compose variant, for local reproduction of the remote-scheduler
topology — not part of the prod deploy).

Darwin currently runs `darwin-kubernetes/background-deployment.yaml`
(a single combined beat+celery+indexer pod via supervisord). **The new
manifests are NOT applied automatically** by `kubectl apply -f
darwin-kubernetes/` because the combined deployment is still in place
— you apply each new file explicitly when you want to switch.

To switch Darwin to the split topology:

```bash
# 1. Apply the new five (order doesn't matter; they self-discover
#    the scheduler Service once it's up).
kubectl apply -f darwin-kubernetes/dask-scheduler-service-deployment.yaml
kubectl apply -f darwin-kubernetes/dask-worker-deployment.yaml
kubectl apply -f darwin-kubernetes/background-beat-deployment.yaml
kubectl apply -f darwin-kubernetes/background-celery-deployment.yaml
kubectl apply -f darwin-kubernetes/background-indexer-scheduler-deployment.yaml

# 2. Wait for all five to be Ready.
kubectl get pods -l 'app in (background-beat,background-celery,background-indexer-scheduler,dask-scheduler,dask-worker)'

# 3. Once healthy + you've seen an indexing attempt dispatch through
#    the new dask-scheduler-service (check the indexer-scheduler
#    pod logs), scale the old combined deployment to 0:
kubectl scale deploy/background-deployment --replicas=0

# 4. If anything goes wrong, scale back up:
kubectl scale deploy/background-deployment --replicas=1
#    The split pods will keep running but no harm — only one set is
#    actually doing the work (whichever has --replicas > 0).
```

Both deployments can coexist briefly during cutover, but **do NOT
run both at non-zero replicas long-term** — two beat schedulers on
the same Postgres broker fire every crontab task twice.

---

## 6. Deployment order

Safe to roll out **in this order, defaults OFF**:

1. Apply the configmap (no behaviour change — flags default OFF):
   ```bash
   kubectl apply -f darwin-kubernetes/env-configmap.yaml
   ```
2. Apply the (possibly updated) secrets:
   ```bash
   kubectl apply -f darwin-kubernetes/secrets.yaml
   ```
3. Apply the Redis StatefulSet:
   ```bash
   kubectl apply -f darwin-kubernetes/redis-statefulset.yaml
   ```
4. Rebuild + push the backend image (so it has `redis` and `bokeh` deps).
5. Rebuild + push the web image (so the UX rewrites ship).
6. Roll out the deployments:
   ```bash
   kubectl rollout restart deploy/api-server-deployment deploy/background-deployment deploy/web-server-deployment
   ```
7. Wait for health, then verify (§7).

**At this point nothing has changed for users** — Redis is up but
nothing is using it, and the api_server / background pods just have
new dependencies + new env vars they're ignoring.

---

## 7. Verification checklist (after deploy, BEFORE flipping flags)

- [ ] All pods healthy: `kubectl get pods -l app=api-server -l app=background -l app=redis`
- [ ] Redis responds: `kubectl exec deploy/redis -- redis-cli PING` → `PONG`
- [ ] api_server logs show no errors importing the new modules
- [ ] `/api/health` returns 200
- [ ] Open `/assistants/mine` — drag handles visible on visible rows, default-pin shows on first row, search input present
- [ ] Open `/assistants/gallery` — sees Yours / Featured sections (and Shared if applicable), filter chips, sort dropdown, columns dropdown
- [ ] Send a chat message — succeeds (proves rate limiter, even though OFF, didn't break the dependency wiring)
- [ ] Background indexer still picks up new indexing attempts (bg-scaling change in `update.py`)

---

## 8. Enabling features (per environment, in any order)

Each flag is independent. Flipping one doesn't require the others.

### 8a. Redis KV cache (settings, tokens, invited users)

```bash
kubectl set env configmap/env-configmap REDIS_KV_CACHE_ENABLED=true
kubectl rollout restart deploy/api-server-deployment
```

**Smoke test:** change an admin setting in pod A, verify it's visible
on pod B within seconds (not TTL).

### 8b. Per-user request rate limit

Pick window values per your traffic shape. Recommended at "few
hundred users" scale:

```bash
kubectl set env configmap/env-configmap \
  REQUEST_RATE_LIMIT_ENABLED=true \
  REQUEST_RATE_LIMIT_PER_MINUTE=20 \
  REQUEST_RATE_LIMIT_PER_HOUR=300
kubectl rollout restart deploy/api-server-deployment
```

**Smoke test:** send 21 chat messages in <60s — the 21st returns 429
with `Retry-After` header.

### 8c. Persona list cache

```bash
# add to the configmap:
PERSONA_CACHE_ENABLED: "true"
PERSONA_CACHE_TTL_SECONDS: "86400"

kubectl apply -f darwin-kubernetes/env-configmap.yaml
kubectl rollout restart deploy/api-server-deployment deploy/background-deployment
```

> Background pod also gets restarted because `ee/danswer/db/user_group.py`
> mutations from there must bust the cache.

**Smoke test:** load `/assistants/mine`, edit one assistant's name in
the admin UI, refresh — the name updates immediately (not on TTL).

### 8d. Bg-scaling Dask topology

Not enabled by env flag — it's a deployment-shape change. Out of
scope for this PR's flip-a-switch flow; if/when adopted, see §5b.

---

## 9. Rollback

Each feature flag flips off independently. The two emergency knobs:

- **Disable a feature flag** (no restart needed for new requests after
  flag propagates):
  ```bash
  kubectl set env configmap/env-configmap REDIS_KV_CACHE_ENABLED=""
  kubectl rollout restart deploy/api-server-deployment
  ```
- **Redis pod dies entirely** — every Redis call in this codebase is
  wrapped fail-open. The app falls back to direct Postgres reads (cache),
  permissive (rate limit), or no invalidation (persona cache;
  worst-case 24h staleness via TTL). **No outage.** Logs will be noisy
  with `Redis GET/SET/DEL failed: …` warnings — that's the signal that
  Redis needs attention.

To roll back the **code** entirely: revert the merge commit, redeploy.
All features default OFF means even without revert, setting all
`*_ENABLED=""` returns the app to pre-PR behaviour.

---

## 10. Known footguns

### 10a. ~~Bg-scaling k8s manifests don't have `REDIS_PASSWORD` wired~~ — RESOLVED

**Closed for the Darwin path.** The 5 ported manifests under
`darwin-kubernetes/` (added in `19335e31`) all wire `REDIS_PASSWORD`
via `secretKeyRef` with `optional: true`, matching the existing
`darwin-kubernetes/background-deployment.yaml` pattern. So persona-
cache invalidation from any future Celery / indexer-scheduler /
dask-worker task path will work correctly once you switch to the
split topology.

The upstream `deployment/kubernetes/*` files are still missing
`REDIS_PASSWORD` env wiring, but **Darwin doesn't apply from that
tree** — it's reference-only (see AGENTS.md "Critical fact §9").
Leave them alone unless/until you adopt the upstream-style
deployment shape outside Darwin.

### 10b. `backend/scripts/seed_assistants.py` bypasses persona-cache invalidation

The seed script writes rows via raw `session.add(Persona(...))` rather
than going through `upsert_persona()`, so `invalidate_personas_all()`
never fires.

- **Today's impact: none** if `PERSONA_CACHE_ENABLED` is the default
  OFF.
- **If you seed with the cache enabled**, `/persona` will keep
  returning the pre-seed list until either a real mutation flows
  through the proper code path or the 24h TTL kicks in. Manual fix:
  ```bash
  kubectl exec deploy/redis -- redis-cli DEL danswer:personas:all:not_deleted
  ```
- **One-line code fix** if this becomes a recurring problem: import
  `invalidate_personas_all` and call it at the end of `main()` in
  `seed_assistants.py`.

### 10c. `update.py` indexing scheduler change needs human eyes

CLAUDE.md flags `update.py` / scheduler changes for manual confirmation
(past breakage was silent — worker died with no logs). The bg-scaling
commit modifies the Dask scheduler / submission path; verify locally
via `python scripts/dev_run_background_jobs.py` and confirm the worker
boots cleanly + dispatches an indexing attempt without errors.

### 10d. Frontend's `chosen_assistants` array can hold stale ids after seed wipe

If you run `python -m scripts.seed_assistants --clear` after seeding,
deleted persona ids may remain in your `User.chosen_assistants` array.
This is harmless — `get_personas` filters out non-existent ids — but
will be cleaned up by the next preference write (any Manage page
reorder / hide / show action).

---

## 11. Manual tests recommended before merge

These need eyes — automated coverage doesn't catch them:

- [ ] **Background worker boots cleanly** on the rebased branch
  (CLAUDE.md gate). `python scripts/dev_run_background_jobs.py`,
  confirm clean startup and an indexing attempt dispatches.
- [ ] **Seed 50 assistants locally**, open `/assistants/mine` and
  `/assistants/gallery`, exercise: drag-reorder, set default, hide via
  toggle, click a hidden row (toggle should pulse), search, bulk
  select, undo from a toast, switch column count in gallery, refresh
  page → column choice persisted.
- [ ] **With KV cache enabled**, edit a setting on pod A while pod B
  is serving — second pod sees the new value without waiting for TTL.
- [ ] **With rate limit enabled**, exceed the per-minute cap; verify
  429 with `Retry-After` header.
- [ ] **With persona cache enabled**, edit an assistant via admin UI;
  `/persona` reflects the edit immediately.

---

## 12. Branch contents at-a-glance

16 commits on top of `rajiv/add-claude` (PR #45):

```
[BG-scale] darwin-kubernetes: port split-background manifests + lock convention in AGENTS.md
[BG-scale] Scale indexing via remote Dask scheduler topology

[Docs]     docs: add MIGRATION.md covering Redis / bg-scaling / UX

[UX]       Gallery: column picker as dropdown to match Sort
[UX]       Gallery: user-controllable column count (segmented control, persists)
[UX]       Show document-set names on assistant cards (was: count only)
[UX]       Parameterize gallery grid column count (default 3)
[UX]       Remove tools chip from Manage Assistants page
[UX]       Assistants UX polish: toggle highlight + gallery declutter
[UX]       Add backend/scripts/seed_assistants.py for local UX testing
[UX]       Assistant Gallery page UX overhaul
[UX]       Manage Assistants page UX overhaul

[Redis]    Persona list cache with explicit write-through invalidation
[Redis]    P2: per-user request rate limiter on chat/query endpoints
[Redis]    P1: Redis foundation + read-through KV cache
[Redis]    docs: add Redis caching & scaling plan
```

Total: **51 files changed, +6372 / −499**. 63 unit tests pass.
