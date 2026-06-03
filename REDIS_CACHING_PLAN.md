# Redis Caching & Scaling Plan

**Goal:** expose the chat interface to a few hundred users. Evaluate and
introduce Redis-based caching where it makes sense, alongside the scaling
work that actually gates that user count.

**Status:** plan only — no code yet. Follows the fork's plan template
(Issues / Important Notes / Strategy / Tests). Treat each phase as an
independently shippable PR.

---

## Context & key findings

- **This fork has zero Redis today.** The only references in the repo are
  comments in `db/index_attempt.py` and `db/retention.py` explaining how
  the fork *avoids* Redis (Postgres advisory locks instead of fences,
  Postgres as the Celery broker). Adding Redis is **net-new infrastructure**,
  which AGENTS.md flags as a substantial dependency, not a drive-by.
- **The real near-term scaling ceiling is the DB connection pool, not the
  DB's query throughput.** `db/engine.py:72` sets `pool_size=40,
  max_overflow=10` → 50 connections per api_server process. `get_session`
  (`db/engine.py:94`) yields one session held for the **whole request**, and
  `/send-message` (`server/query_and_chat/chat_backend.py:276`,
  `handle_new_chat_message`) returns a `StreamingResponse` — so a connection
  is pinned for the entire LLM stream (10–60s). At a few hundred users this
  exhausts the pool before query volume ever stresses Postgres. **No cache
  fixes this.**
- **A rate limiter already exists but is the wrong kind.**
  `server/query_and_chat/token_limit.py::check_token_rate_limits` enforces a
  *token-budget* limit (global, DB-backed, EE-overridable). It is not a
  *request-rate* limiter and `any_rate_limit_exists()` is gated by a
  per-process `@lru_cache` (`token_limit.py:122`) that won't reflect changes
  across replicas.
- **The highest-leverage cache seam already exists:** the
  `DynamicConfigStore` abstraction (`dynamic_configs/interface.py`,
  `store.py`, `factory.py`) is the fork's equivalent of upstream's
  `PgRedisKVStore`. Wrapping it gives transparent, write-through-invalidated
  caching for everything routed through it with zero call-site changes.

### Non-goals (explicitly out of scope)

- **Do not** move the Celery broker to Redis (stays on Postgres — deliberate
  divergence).
- **Do not** replace indexing advisory-lock fences with Redis fences.
- **Do not** cache chat sessions / messages (too mutable; correctness risk).
- **Do not** cache LLM/embedding *responses* (semantic/correctness risk).
- **Do not** add tenant key-prefixing — this fork is single-tenant.

---

## Phase summary

| Phase | What | Caching? | Gates the user count? |
|---|---|---|---|
| **P0** | Connection-pool / session-holding fix + multi-replica | No | **Yes — do first** |
| **P1** | Redis foundation + `DynamicConfigStore` read-through cache | Yes (flagship) | Enables the rest |
| **P2** | Redis-backed per-user request rate limiting | No (protection) | Yes, for cost/abuse |
| **P3** | Per-chat-turn config caches (LLM provider, embedding settings) | Yes | Measured add-on |
| **Opt** | Document sets, connector OAuth/API caches, Redis sessions | Yes | Situational |

---

## P0 — Connection pool & session lifetime (prerequisite, not caching)

### Issues to address
At a few hundred users, concurrent streaming chats pin all 50 connections
per process; unrelated (even cached) requests then queue. This is the first
thing that breaks.

### Important notes
- `handle_new_chat_message` holds `Depends(get_session)` for the full
  `StreamingResponse`. The fix is to scope DB work to *before* the stream
  starts (load everything needed, commit the user message), then run the
  stream without a pinned pooled connection, opening short-lived sessions
  only for the final persistence write.
- This touches the core chat path — **per CLAUDE.md, confirm with the human
  and verify the worker/stream boots cleanly** before/after. High blast
  radius; ship as its own PR with manual load verification.
- Independently: run **multiple api_server replicas** (k8s) behind nginx,
  and size `pool_size` against Postgres `max_connections` ÷ replica count.

### Implementation strategy
1. Audit `handle_new_chat_message` and the `process_message` generator for
   what truly needs the session during streaming vs. before it.
2. Introduce a pattern where the streaming generator uses
   `get_session_context_manager()` for short writes rather than the
   request-scoped `Depends(get_session)`.
3. Bump replica count in
   `darwin-kubernetes/api_server-service-deployment.yaml`; re-tune pool.

### Tests
- Load test: N concurrent streaming chats (N > pool size) — confirm
  non-chat endpoints (settings, session list) stay responsive.
- Verify no `QueuePool limit ... connection timed out` under load.

---

## P1 — Redis foundation + DynamicConfigStore cache (flagship)

### Issues to address
Cache the highest-frequency, fires-on-every-page reads (settings, tokens,
invited users) at one central, low-risk seam, with correct cross-replica
invalidation.

### Important notes
- Mirror upstream's `PgRedisKVStore` *shape* but fit this fork's interface:
  `store(key, val, encrypt)` / `load(key)` / `delete(key)` raising
  `ConfigNotFoundError` (`dynamic_configs/interface.py`).
- Write-through invalidation is **free** here — the same `store()`/`delete()`
  that writes Postgres updates/clears Redis, so all replicas see changes.
- Single-tenant → plain key prefix (e.g. `danswer_kv:`), no tenant wrapper.
- Redis must be **fail-open**: if Redis is down, fall back to Postgres so an
  outage degrades latency, not availability.

### Implementation strategy
1. **Dependency:** add `redis==<pin>` to `backend/requirements/default.txt`.
2. **Config:** add `REDIS_HOST/REDIS_PORT/REDIS_PASSWORD/REDIS_DB_NUMBER`
   to `configs/app_configs.py` via the existing `os.environ.get` pattern;
   add a `REDIS_KEY_VALUE_CACHE_TTL` (default ~1 day, mirroring upstream).
3. **Client module:** new `backend/danswer/redis/redis_pool.py` — a
   `ConnectionPool` singleton + `get_redis_client()`. (Upstream's
   `redis_pool.py` is the template, minus IAM/tenant code.)
4. **Cache layer:** add `CachedDynamicConfigStore` (decorator/wrapper around
   `PostgresBackedDynamicConfigStore`) in `dynamic_configs/store.py`, or add
   Redis read-through directly to the PG store. `load()` checks Redis →
   misses fall to PG and repopulate; `store()`/`delete()` write PG then
   set/clear Redis. Route it via `dynamic_configs/factory.py`
   (`get_dynamic_config_store`) behind a `DYNAMIC_CONFIG_STORE` value so it's
   toggleable.
5. **Deployment:** Redis statefulset + service in `darwin-kubernetes/`;
   redis service in `deployment/docker_compose/docker-compose.dev.yml`;
   wire env in `env-configmap.yaml` + password in `secrets.yaml`.

### What this transparently caches
Everything through `get_dynamic_config_store()`: app settings
(`server/settings/store.py`, key `danswer_settings`), Slack bot tokens,
invited users, telemetry id, Gmail/GDrive connector-auth blobs.

### Tests
- Unit: `load` hits Redis on 2nd call (mock PG, assert one PG query);
  `store`/`delete` invalidate; Redis-down path falls back to PG (fail-open).
- Integration: change settings via the admin endpoint → second replica (or
  fresh client) reads the new value without a TTL wait.

---

## P2 — Redis-backed per-user request rate limiting (protection)

### Issues to address
A few hundred users on chat = real risk of runaway LLM **cost** and hitting
the **provider's** rate limits. Need per-user request-rate limiting that is
correct across replicas (in-memory counters let through ~N× at N pods).

### Important notes
- This **complements**, does not replace, the existing token-budget limiter
  in `token_limit.py`. Keep that; add request-rate limiting on top.
- Also fixes the latent multi-replica issue: the per-process `@lru_cache` on
  `any_rate_limit_exists()` (`token_limit.py:122`) can be made Redis-backed
  or given a short TTL so all pods agree.
- No rate-limit middleware exists today (only `latency_logging.py`) — this is
  net-new. fastapi 0.109.2 is compatible with `fastapi-limiter` or a small
  custom `incr`+`expire` limiter.

### Implementation strategy
1. Add a Redis counter limiter: key `ratelimit:msg:{user_id}:{bucket}`,
   atomic `incr` + `expire(window, NX)` (or a small Lua script for
   multi-tier limits). Reuse the P1 `redis_pool` client.
2. Apply at the chat entrypoint (`/send-message`) as a dependency, before any
   LLM work; raise `HTTPException(429)` (this fork uses `HTTPException`, not
   `OnyxError`).
3. Make limits env-configurable in `configs/app_configs.py` (per-user
   per-minute / per-hour). Default off via env so it's opt-in per environment.

### Tests
- Unit: counter increments/expires; exceeds → 429.
- Integration: two clients simulating two replicas share the same limit
  (single Redis), confirm the aggregate cap holds.

---

## P3 — Per-chat-turn config caches (measured add-on)

### Issues to address
Fired on every chat turn × hundreds of users → meaningful aggregate even
though each query is cheap.

### Important notes
- **Cache the serialized Pydantic snapshot, not the ORM object** — these
  return SQLAlchemy models with lazy relationships; caching the ORM instance
  risks `DetachedInstanceError` / stale relationship reads.
- Invalidation is **not** free here (unlike P1) — must add explicit
  bust/refresh calls inside the relevant `db/` mutation functions. This is
  the added surface area; only do it after P0/P1 and after measuring.

### Implementation strategy
- **Default LLM provider:** cache `db/llm.py::fetch_default_provider` /
  `fetch_existing_llm_providers`; invalidate in the provider create/update/
  delete paths in `db/llm.py` and the admin endpoint.
- **Current embedding/search settings:** cache
  `db/embedding_model.py::get_current_db_embedding_model`; invalidate on
  index-swap (when a new `EmbeddingModel` becomes `PRESENT`).
- Use short TTLs as a backstop even with explicit invalidation.

### Tests
- Unit: cached fetch returns snapshot; mutation path clears it.
- Integration: change default provider → chat picks it up without restart.

---

## Optional / deferred

| Item | Where | Note |
|---|---|---|
| **Document sets** | `db/document_set.py::fetch_document_sets` | Global key in this fork (base version ignores `user_id`); write-through on the ~5 mutation fns. Admin-page frequency, modest win. |
| **Connector OAuth / external-API caches** | per-connector (cf. upstream Confluence/Slack) | Only if those connectors are active; cuts external rate-limit pressure. Short TTL. |
| **Redis auth sessions** | `auth/users.py` (fastapi-users RedisStrategy) | Offloads per-request auth from Postgres; bigger change + security/invalidation care. Defer until auth DB load shows up. `SESSION_EXPIRE_TIME_SECONDS` already exists. |
| **Personas list** | — | **Skip backend cache** (per-user + group-membership invalidation trap). Use frontend (SWR) caching instead. |

---

## Cross-cutting

### New files / touched files
- New: `backend/danswer/redis/redis_pool.py`, Redis k8s manifests.
- Touched: `requirements/default.txt`, `configs/app_configs.py`,
  `dynamic_configs/store.py`, `dynamic_configs/factory.py`,
  `docker-compose.dev.yml`, `darwin-kubernetes/{env-configmap,secrets}.yaml`,
  `api_server-service-deployment.yaml` (replicas). P2/P3 touch
  `chat_backend.py`, `token_limit.py`, `db/llm.py`, `db/embedding_model.py`.

### Restart / bounce list (per CLAUDE.md)
- New env vars / requirements → rebuild + restart api_server (`dapi`),
  background jobs (`dbe`), Slack listener (`dsl`).
- `redis` dependency add → `pip install` in the venv before running.

### Open questions for the human
1. **P0 session-refactor sign-off** — high blast radius on the chat path;
   confirm approach + manual load verification before merge.
2. Redis deployment shape in `darwin-kubernetes` — single statefulset vs.
   managed Redis? Persistence needed (cache-only ⇒ probably not)?
3. Default rate-limit values for P2 (per-user/min, per-user/hour).
4. Sequencing: is P0 acceptable to do in parallel with P1, or strictly first?
