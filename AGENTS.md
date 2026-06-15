# AGENTS.md

Operating notes for AI coding agents working in this repo. Read this before
making non-trivial changes — it captures the architecture facts that aren't
obvious from a top-down read of the code, plus the footguns this codebase
has bitten previous sessions with.

If you're a human contributor, this is also a useful onboarding companion
to `CONTRIBUTING.md` (which is more about *how to set up*; this is more
about *how the system actually works*).

> **⚠️ Read this before applying anything you've seen in upstream Onyx.**
> This repo is a Danswer fork, branded "Darwin" internally, that diverged
> from upstream roughly two years ago. Onyx
> ([github.com/onyx-dot-app/onyx](https://github.com/onyx-dot-app/onyx))
> has since been substantially rewritten — different background-worker
> architecture, different error-handling conventions, different package
> name, different LLM-tracing system, different multi-tenant model. Many
> of upstream's `AGENTS.md` / `CLAUDE.md` rules will actively mislead you
> here. See **"Divergence from upstream Onyx"** below for a concrete
> mapping of what does and doesn't apply.

---

## What this repo is

- **Origin**: fork of [Danswer](https://github.com/danswer-ai/danswer)
  (now renamed [Onyx](https://github.com/onyx-dot-app/onyx) upstream).
  This fork is **roughly two years behind upstream** on most modules.
- **Branding**: shipped internally as **"Darwin"** — that name appears
  in UI labels, connector name builders, and a few comments. The Python
  package is still called `danswer/`; don't rename it unless you mean to.
- **Working branch**: `feature/darwin`. `main` exists but new work lives
  on `feature/darwin`.
- **Deployment shape**: monorepo with `backend/` (Python 3.11, FastAPI),
  `web/` (Next.js 14 App Router, React 18, TypeScript), and
  `deployment/docker_compose/` for local + prod stacks.

---

## Architecture in one paragraph

A FastAPI backend serves an admin / search UI written in Next.js. Data
sources are pulled by **connectors** (one Python class per source type)
on a fixed cadence into **Vespa** for vector + BM25 search and into
**Postgres** for relational metadata. Indexing runs on a **Dask local
cluster** spawned by `dev_run_background_jobs.py`. **Celery** (with a
SQLAlchemy/Postgres broker) handles a few maintenance tasks — cleanup,
prune, document-set sync, user-group sync — but **does not run indexing**.
LLM calls go through a configurable gateway (this fork defaults to a
UiPath OAuth-secured custom endpoint).

---

## Divergence from upstream Onyx

The single most likely way to break this codebase is to copy a pattern
from upstream's `AGENTS.md` / `CLAUDE.md` and apply it here. Upstream has
moved on substantially. This table is the explicit map.

| Concept | Upstream Onyx | This fork |
|---|---|---|
| Package name | `onyx/` | `danswer/` (kept the original) |
| Indexing runtime | Celery `docfetching` + `docprocessing` workers | **Dask `LocalCluster`** in `update.py` (Celery only does maintenance) |
| Number of Celery workers | Eight specialized workers (primary, light, heavy, kg_processing, monitoring, beat, etc.) | One worker + beat, spawned by `dev_run_background_jobs.py` |
| Celery task definition | `@shared_task` under `background/celery/tasks/` | `@celery_app.task` in `background/celery/celery_app.py` |
| Celery broker | Redis | SQLAlchemy/Postgres by default; **optionally Redis** via `CELERY_BROKER_REDIS_ENABLED=true` (logical DB `CELERY_REDIS_DB_NUMBER`, default 1). Prod enables it to keep Celery's queue traffic off Postgres. Indexing is still Dask either way. |
| Error handling | `raise OnyxError(OnyxErrorCode.X, …)` everywhere; no `HTTPException` | Plain `HTTPException(status_code=…, detail=…)` is the norm here. `OnyxError` doesn't exist. |
| FastAPI return types | "Don't use `response_model=`, just type the function" | Both styles exist in this fork (the typed-return-annotation form is the majority — `response_model=` only appears once in `connector.py:560`). New endpoints should use the typed-return form. Don't strip the existing `response_model=` without checking serialization behavior. |
| LLM call instrumentation | Every call must open a `LLMFlow`-tagged span via `traced_llm_call(...)` | No tracing system. `LLMFlow` doesn't exist. |
| Multi-tenancy | `alembic -n schema_private upgrade head`; `DynamicTenantScheduler` | Single-tenant. `alembic upgrade head` is the only form. |
| Knowledge Graph processing | Full pipeline (`kg_processing` worker, clustering) | Doesn't exist |
| Logs | `backend/log/<service_name>_debug.log` (services tail to per-file logs) | No `backend/log/` dir; logs go to stdout of each process |
| Postgres container name | `onyx-relational_db-1` | `danswer-stack-relational_db-1` (only when started with `docker compose -p danswer-stack`, which `CONTRIBUTING.md` mandates; if you skipped `-p`, run `docker ps` to confirm the actual name) |
| Test buckets | `backend/tests/{unit,external_dependency_unit,integration}` + Playwright e2e | No comparable structure here. Most code lacks tests; add tests with the change if practical, otherwise note in PR. |
| Plan template | The "Creating a Plan" section in their `CLAUDE.md` (Issues / Notes / Strategy / Tests) | Useful template; can be borrowed for non-trivial changes here too. |
| Frontend stack | Next.js 15+, React 18+ | Next.js 14.2.x (App Router), React 18 |
| K8s manifest path | `deployment/kubernetes/*` is what upstream documents | **`darwin-kubernetes/*` is the source of truth for the Darwin prod cluster.** `deployment/kubernetes/*` is upstream legacy / scratch — Darwin doesn't apply from there. New manifests for Darwin go in `darwin-kubernetes/`. See critical fact §9. |

**Rule of thumb when reading upstream code or upstream guidance:** assume
it doesn't apply unless you can verify the same construct exists here.
`grep` for the construct in `backend/danswer/` first. If it's missing,
don't introduce it as part of an unrelated change — that's a substantial
new dependency, not a drive-by.

**When upstream guidance does still apply:**

- **Type strictness** (Python with mypy, TypeScript). Both projects are
  fully type-annotated and we keep them that way.
- **DB ops live in `db/` directories.** Don't write SQL or ORM queries
  outside `backend/danswer/db/` (or `backend/ee/danswer/db/` for EE
  features).
- **Never commit secrets.** Encrypted credential storage for connector
  creds; OAuth client_id/secret in env, not source.
- **Frontend → backend goes through the frontend's proxy at `:3000/api/...`,
  not directly to `:8080`** when you're testing UI flows. The proxy adds
  cookies / auth headers the bare backend doesn't know about.
- **`source .venv/bin/activate`** if you hit ImportErrors. Same trick.
- **The plan-writing template** (Issues / Important Notes / Strategy / Tests,
  no rollback / timeline) is a good shape for non-trivial work here too.

---

## Top-level layout

```
backend/
  danswer/
    background/
      celery/                      ← Celery app + maintenance tasks (NOT indexing)
      connector_deletion.py        ← cleanup_connector_credential_pair_task body
      indexing/                    ← indexing pipeline (chunking, embeddings)
      update.py                    ← MAIN INDEXING LOOP — Dask LocalCluster
    configs/constants.py           ← DocumentSource enum (every connector adds here)
    connectors/
      <source>/connector.py        ← one folder per connector type
      factory.py                   ← DocumentSource → connector class registry
      models.py                    ← Document / Section / BasicExpertInfo
    db/
      models.py                    ← SQLAlchemy: Connector, Credential,
                                     ConnectorCredentialPair, IndexAttempt,
                                     TaskQueueState, Document, ...
      index_attempt.py             ← scheduling helpers
      credentials.py
      tasks.py                     ← Celery task tracking helpers
    document_index/vespa/index.py  ← Vespa client (read + write)
    server/
      documents/
        connector.py               ← /api/manage/admin/connector/* routes
        credential.py              ← /api/manage/credential/* routes
        cc_pair.py                 ← /api/manage/admin/cc-pair/{id} routes
        models.py                  ← Pydantic snapshots (ConnectorIndexingStatus,
                                     IndexAttemptSnapshot, CCPairFullInfo, ...)
      manage/                      ← admin housekeeping endpoints
    danswerbot/slack/listener.py   ← Slack bot (separate process)
  alembic/versions/                ← migrations (single-head expected)
  scripts/
    dev_run_background_jobs.py     ← spawns Celery worker + beat + Dask indexer
    list_salesforce_account_fields.py
    preview_salesforce_accounts.py
    dump_salesforce_account.py     ← Salesforce-specific dev tooling

web/src/
  app/admin/
    add-connector/                 ← connector tile gallery (sources.ts feeds it)
    connector/[ccPairId]/          ← per-cc-pair detail page (status, credential
                                     editor, indexing attempts, delete)
    connectors/<source>/           ← per-source-type setup page (Step 1: creds,
                                     Step 2: manage). Folder name = derived URL.
    indexing/status/               ← "Existing Connectors" list with bulk filter / edit
  components/admin/connectors/
    ConnectorForm.tsx              ← config form
    CredentialForm.tsx             ← credential form (supports edit mode via
                                     existingCredentialId prop)
    table/
      ConnectorsTable.tsx          ← used by per-source-type pages
      DeleteColumn.tsx             ← reads is_deletable from indexing-status response
  lib/
    types.ts                       ← TS mirror of Pydantic snapshots
    sources.ts                     ← tile metadata (icon, displayName, adminUrl?)
    connector.ts                   ← connector REST client
    credential.ts                  ← credential REST client (createCredential,
                                     updateCredential PATCH, linkCredential)

deployment/docker_compose/
  docker-compose.dev.yml           ← local stack (relational_db + index/Vespa +
                                     api_server + web_server + model_server +
                                     background + nginx). Celery brokers on
                                     Postgres by default, or Redis when
                                     CELERY_BROKER_REDIS_ENABLED=true.
```

---

## Critical facts that bite

These are the non-obvious things previous sessions wasted hours on.
Read carefully.

### 1. Indexing is on Dask, NOT Celery

`update.py` spawns a `dask.distributed.LocalCluster` (or `SimpleJobClient`
in dev) and submits indexing attempts as Dask futures via
`client.submit(run_indexing_entrypoint, attempt_id, ...)`. Celery in this
codebase only runs the maintenance tasks listed in `celery_app.py`:
`cleanup_connector_credential_pair_task`, `prune_documents_task`,
`sync_document_set_task`, `check_for_*` periodic tasks.

If you need to change indexing behavior (priority, queueing, concurrency),
**look at `update.py` and Dask docs**, not Celery / Kombu.

**Concurrency**: `NUM_INDEXING_WORKERS` env var controls Dask cluster
size; default is 1. Safe to scale to N>1. Two independent guards prevent
the failure modes that show up beyond a single worker:

1. **Per-cc-pair collision guard** — two layers, both leave the row as
   `NOT_STARTED` (never FAILED), so the indexing-status table never
   accumulates "skipped" failure rows for routine deferral:
   - Scheduler-side: `update.py::kickoff_indexing_jobs` defers any
     NOT_STARTED attempt whose `(connector, credential, embedding_model)`
     tuple already has an IN_PROGRESS attempt. Catches the common case
     (manual Re-Index colliding with an auto-scheduled run).
   - Worker-side: `try_acquire_cc_pair_lock` (Postgres advisory lock,
     session-scoped) covers the true-race case where two NOT_STARTED
     rows are submitted in the same scheduler tick. If the lock fails,
     the worker **reverts the attempt to NOT_STARTED** (clears
     `time_started`) so the next tick re-dispatches it.
   Replaces upstream Onyx's per-cc-pair Redis fence pattern.

2. **Scheduler-side per-source-type cap**
   (`background/update.py::kickoff_indexing_jobs`, configured by
   `configs/indexing_concurrency.py`): generic cap of
   `INDEXING_PER_SOURCE_CAP` (default `1`) attempts per `DocumentSource`
   at a time. Before submitting each NOT_STARTED attempt to Dask, the
   scheduler counts IN_PROGRESS attempts for the same source and defers
   anything that would push it over the cap. Deferred attempts stay
   NOT_STARTED — no FAILED rows, no extra `error_msg`. With
   `NUM_INDEXING_WORKERS=4` + four GitHub cc-pairs: one runs, three sit
   in NOT_STARTED until the running one finishes. Every source type is
   its own bucket; connectors that share an external credential (e.g.
   `github` + `github_files` share a PAT) are not collapsed — fold them
   into a single `DocumentSource` if that matters for your rate limits.
   Set `INDEXING_PER_SOURCE_CAP=0` to disable capping entirely.

When scaling `NUM_INDEXING_WORKERS`, also bump model-server worker
count and Postgres `max_connections` — see CONTRIBUTING.md "Scaling
indexing concurrency" + "Per-Source Indexing Concurrency Caps" for the
full operational checklist.

### 2. `dev_run_background_jobs.py` swallows subprocess errors

The dev script spawns Celery worker + beat as `subprocess.Popen` and only
echoes their stdout. **There's no return-code check.** If the worker dies
at boot (wrong `-A`, broker unreachable, bad `--pool` combo), you'll see
the error once in the WORKER: log lines, then `monitor_process` exits
quietly while the parent script keeps running indexing fine — but no
maintenance tasks ever execute, and `task_queue_jobs` rows pile up forever
in `PENDING`.

Before debugging "stuck deletion / sync / prune" issues, **always confirm
the worker is alive**: `ps aux | grep '[c]elery.*worker'`. If it's not
there, fix whatever's wrong with the boot args and restart the script.

The current correct `-A` value is `ee.danswer.background.celery.celery_app`
(the module path, not the package — `__init__.py` is empty so the package
form fails with "no attribute 'celery'").

### 3. Long-running processes must restart after enum additions

Adding a value to `DocumentSource` (or any string enum used in
Pydantic models) is a **breaking change for in-memory consumers**. If the
indexer writes a document with `source_type = "github_files"` to Vespa
while the API server / Slack listener is still running with the *old*
enum loaded in memory, every read of that document will fail with a
Pydantic `ValidationError: source_type` once the new value comes back
from Vespa.

Process restart matrix after enum additions:

| Process | How it's started | Why it needs restart |
|---|---|---|
| Indexer | `python scripts/dev_run_background_jobs.py` | Constructs Documents with the new source. Restart to register. |
| API server | `uvicorn danswer.main:app …` | Deserializes Vespa results. |
| Slack listener | `python danswer/danswerbot/slack/listener.py` | Same as API. |
| Celery worker / beat | spawned by the dev script | Imports `connectors/factory.py`. |
| Frontend (`npm run dev`) | Hot-reloads modules but `.next/cache` can lag — `rm -rf web/.next` if a tile/source rename doesn't show. Also: `next.config.js` is only re-read on full restart. |

Auth-specific bounces (orthogonal to enum changes):

| Edit | Bounce |
|---|---|
| `AUTH_TYPE`, `OPENID_CONFIG_URL`, `OAUTH_CLIENT_*`, `USER_AUTH_SECRET`, `DEFAULT_ADMIN_EMAILS` | API server (`dapi` / `dapi_oidc`). Env is captured at fork time; `--reload` doesn't refresh it. Also: `--reload` only re-imports module code, not the module-level constants like `AUTH_TYPE = AuthType(os.environ.get(...))` that already evaluated. Hard restart. |
| `web/next.config.js` (rewrites / redirects) | Frontend (`dfe`). Next.js reads this file once at boot. |

### 4. The list endpoint serves both pages

`/api/manage/admin/connector/indexing-status` is consumed by:
- `/admin/indexing/status` (the list, via `CCPairIndexingStatusTable`)
- *Every* per-source-type page (e.g. `/admin/connectors/sf-account`,
  via `ConnectorsTable` + `DeleteColumn`).

Both deserialize the same `ConnectorIndexingStatus[]` payload. So fields
in the response can't be removed unilaterally — `is_deletable` for
example is only read by `DeleteColumn`, but it's still required because
that column lives on the per-source-type pages.

When optimizing this endpoint, **compute fields inline from data already
in memory** (latest_index_attempt, connector.disabled) rather than
removing fields. The current implementation in
`server/documents/connector.py::get_connector_indexing_status` does
this — bulk-fetches `latest_index_attempts`, `document_count_info`, and
`cleanup_task_by_name` up front, then computes `is_deletable` and
`deletion_attempt` per row from those dicts. Total query count is O(1)
in the number of cc_pairs. Don't re-introduce per-row helper calls.

### 5. Frontend admin URL is auto-derived from displayName

`web/src/lib/sources.ts::fillSourceMetadata` builds `adminUrl` as
`/admin/connectors/${displayName.toLowerCase().replaceAll(" ", "-")}`
*unless* the entry overrides `adminUrl`. So if you change a tile's
`displayName` and the auto-derived URL no longer matches the folder
under `web/src/app/admin/connectors/`, **the tile clicks land on a 404**.

Three options when renaming:
1. Change displayName + rename the folder to match.
2. Change displayName + add an explicit `adminUrl: "/admin/connectors/<old>"`
   override on the source entry.
3. Don't change displayName.

### 6. Credentials are stored as opaque JSON, no per-source schema

The `credential` table has a `credential_json: dict` column. The backend
`update_credential` does a wholesale replace — there's no merge, no
field-level update. So edit forms must submit the full credential JSON
including any discriminator fields (e.g. `sf_credential_kind`) or those
get wiped on save.

### 7. Salesforce SOQL date format

SOQL accepts both `Z` and `+0000` timezone suffixes, but **`+00:00`
(with the colon) sometimes survives URL-encoding into the query string
as a space**, silently turning the WHERE clause into a no-match. Always
format dates as `%Y-%m-%dT%H:%M:%S.000Z` — see `_soql_datetime` in
`connectors/salesforce/connector.py` for the helper.

### 8. Office365 auto-parses `application/json` files in SharePoint

The `office365-rest-python-client==2.5.9` library inspects the response
Content-Type — if a SharePoint file's response comes back as
`application/json` (which happens for any `.json` file in the drive),
the library parses the body into Python objects (list / dict) and
populates `driveitem.get_content().execute_query().value` with that,
instead of bytes. So passing `.value` directly to `io.BytesIO(...)`
crashes with `TypeError: a bytes-like object is required, not 'list'`
on JSON files.

Past attempts to work around this in the connector were reverted (the
re-serialized text isn't byte-for-byte faithful since the library has
already lost the original whitespace / key order). If you're indexing
JSON files via SharePoint, the right fix is bypassing office365's
auto-parse entirely with a raw `requests.get` against the
`/drives/{drive_id}/items/{item_id}/content` endpoint using the bearer
token. Don't reintroduce the lossy re-serialization.

### 9. `darwin-kubernetes/` is the source of truth for the Darwin cluster

The repo has two parallel k8s manifest trees and they are **not** kept
in sync:

| Path | What it is | When to touch |
|---|---|---|
| `darwin-kubernetes/*.yaml` | **The actual manifests applied to Darwin's AKS cluster (the `darwin` kube context).** Image registry is `sfbrdevhelmweacr.azurecr.io/...`, configmap is `env-configmap`, secrets is `danswer-secrets`, indexing pods have `indexcpu`-pool affinity + `darwin/indexing` toleration, env vars come from the Darwin configmap. | **Edit here for any prod-affecting change**, including new deployments. |
| `deployment/kubernetes/*.yaml` | Upstream-style manifests inherited from Onyx / authored to match the OSS docker-compose. Generic image (`danswer/danswer-backend:latest`), no Azure-specific affinity / tolerations, no Darwin-specific configmap wiring. | Reference only — not deployed to Darwin. Useful for seeing the "upstream shape" of a new component before adapting it to `darwin-kubernetes/`. |

When upstream (or a branch like `feature/backgroundscaling`) adds a
new manifest in `deployment/kubernetes/`, the corresponding
`darwin-kubernetes/` version must be hand-ported with:

- Image: `sfbrdevhelmweacr.azurecr.io/danswer/danswer-backend:<tag>`
- `envFrom: configMapRef name: env-configmap`
- POSTGRES_USER / POSTGRES_PASSWORD via `secretKeyRef name: danswer-secrets`
- REDIS_PASSWORD via `secretKeyRef name: danswer-secrets, optional: true`
  (so unauth'd in-cluster Redis still works)
- For indexing-related pods: `nodeAffinity` on `agentpool=indexcpu` +
  `tolerations` for `darwin/indexing/NoSchedule` + `dynamic-pvc` /
  `file-connector-pvc` volume mounts.

A drop-in port that misses any of these will boot in Darwin but
mis-route, miss secrets, or end up on the wrong node pool. The
existing `darwin-kubernetes/background-deployment.yaml` and
`api_server-service-deployment.yaml` are the canonical templates for
the conventions.

### 10. NEVER use `:latest` (or a floating tag) for Vespa — pin the exact version

**This caused a full prod outage.** Vespa's config server refuses an
auto-upgrade spanning more than ~30 releases (`VersionState
.verifyVersionIntervalForUpgrade` → `Cannot upgrade from X to Y ...
interval too large`). If a manifest change bumps the Vespa image to a
much newer version, **every Vespa StatefulSet rolls and the config
server crash-loops on bootstrap**, taking the whole cluster down
(config tier → no quorum → cluster-wide `upstream connect error /
connection refused` 503s on search AND the api-server's
`ensure_indices_exist`).

What triggered it: an image spec of bare `vespaengine/vespa` (which
pulls `:latest` at pull time) was changed to an explicit
`vespaengine/vespa:latest`, and on the next `kubectl apply` `:latest`
had moved 30+ releases ahead of the running version.

Rules:
- **Pin Vespa to the exact version the cluster runs.** As of this
  writing that is **`8.600.35`** — it's the on-disk format the content
  nodes' index (1.6M+ docs, 100Gi PVCs) is written in. See the pinned
  `images:` entry + comment in `k8s/overlays/{prod,local}/kustomization.yaml`.
- **Upgrades are STEPWISE and deliberate** — at most ~30 releases per
  hop, applied as an ordered operation, never a bare tag bump. Do NOT
  set `VESPA_SKIP_UPGRADE_CHECK=true` to force a big jump on prod; it
  risks the index format.
- **Upgrade with `k8s/scripts/vespa-upgrade.sh <target> [ns]`, NOT a
  kustomize apply.** Ordering across the 5 StatefulSets (configserver →
  admin → content one-ordinal-at-a-time → feed → query, health-gated
  between each) is impossible to express declaratively — a `kubectl
  apply` rolls them all at once. The manifests support the script via
  **per-role logical image names** (`vespa-configserver`, `vespa-admin`,
  `vespa-content`, `vespa-feed`, `vespa-query` in `k8s/base/vespa/`) so
  versions move independently, plus readiness probes on content/admin
  with `publishNotReadyAddresses: true` on `vespa-internal` (peer
  discovery must not be readiness-gated). Run `DRY_RUN=1` first. After a
  successful upgrade, sync the per-role `newTag`s in the overlays.
- **`k8s/scripts/guarded-apply.sh <overlay>` is the everyday-apply safety
  net, not the upgrade tool.** The guard reads the live running Vespa
  version, compares it to what the overlay would deploy, and refuses a
  >30-minor upgrade / major change / floating tag (and warns on big
  downgrades) before it can reach the cluster. It checks against *live*
  (not the repo's previous pin) because config drifts out of git. But it
  still rolls all roles at once — for an actual version change use
  `vespa-upgrade.sh`.
- This applies to any version-stateful StatefulSet, but Vespa is the
  one that bites.

**Recovery if it happens again** (data is safe — it lives on the
content PVCs, untouched): set all 5 Vespa StatefulSets' image back to
the running version (`kubectl set image statefulset/vespa-* ...`),
delete the config-server pods to recreate on the correct version, wait
for `:19071/state/v1/health` → 200, then restart the api-server so
`ensure_indices_exist` redeploys the schema. (Clearing the
config-server ZooKeeper state via `vespa-configserver-remove-state` is
only needed if the ZK state is genuinely corrupt — the version
mismatch alone does NOT require it.) Vespa nodes also have **no
liveness probes by design** (an aggressive one kills slow-but-healthy
nodes); readiness probes on the Service-backed nodes
(configserver/query/feed) gate traffic during the slow bootstrap.

### 11. Slow indexing is the per-doc Vespa existence VISIT, not the crawl — and the content-hash dedup already exists

When a connector (especially a big web one like docs.uipath) indexes slowly,
the bottleneck is almost never the source fetch. For every document,
`VespaIndex.index()` → `_clear_and_index_vespa_chunks()` →
`_get_vespa_chunks_by_document_id` (`document_index/vespa/index.py`) hits Vespa's
**Visit API** (`GET /document/v1/.../docid?selection=document_id=='<id>'&wantedDocumentCount=1000`)
to find existing chunks before re-writing. That `selection` is a **corpus scan**,
~**10–11 s per document** on the large prod index — and Vespa content nodes sit
near-idle while it happens (it's scan/IO-bound, so scaling content nodes does
NOT help). It's in the shared indexing path, so it slows every connector; large
multi-doc connectors just make it obvious. The real fix is a keyed lookup
(`document_id` as a `fast-search` attribute, or a point GET / search query)
instead of the visit.

**Do NOT "add" a Postgres content-hash dedup to skip this — it already exists.**
`Document.indexed_content_hash` (`db/models.py`) +
`get_doc_ids_to_update` (`indexing/indexing_pipeline.py`) skip a doc (no re-embed,
no Vespa write) when the stored hash equals `doc.get_content_hash()`. The hash is
written only AFTER a confirmed Vespa write. Why it can still re-index everything:

- It's bypassed when `ignore_time_skip=True`, set on `from_beginning` full runs
  (`background/indexing/run_indexing.py`).
- Docs indexed before the hash feature have `indexed_content_hash = NULL`, so the
  hash check can't fire and it falls back to a `doc_updated_at` timestamp compare.
- The **web connector never sets `doc_updated_at`**, so that fallback can't skip
  hash-less web docs either → they re-index every run (each paying the ~11 s
  visit) UNTIL the run completes and backfills their hash. It is self-healing —
  once hashes exist, later polls skip unchanged docs and run fast — but a full
  run that times out before backfilling will keep re-doing the slow work.

(Diagnosed 2026-06 on the docs.uipath automation-suite latest-N connector:
~2889 of ~3161 docs had NULL hashes.)

---

## Common workflows

### Add a new connector

Verified against the most recent connector addition (`github_files`).
Touch every one of the files below; missing any of them produces silent
or hard-to-diagnose failures (404 tile, factory KeyError, frontend type
errors, or — worst — the connector silently registering as the wrong
type).

**Backend**

1. **Create the package directory** under
   `backend/danswer/connectors/<source>/` with two files:
   - `__init__.py` — empty (required so Python treats it as a package).
   - `connector.py` — your connector class implementing one or more of
     the abstract bases in `backend/danswer/connectors/interfaces.py`:
     `LoadConnector` (full pull), `PollConnector` (incremental by time
     window), `IdConnector` (cheap doc-id enumeration for prune).
     Raise `ConnectorMissingCredentialError` (from
     `connectors/models.py`) when `load_credentials` hasn't been called.
     Clone `connectors/github_files/connector.py` as the most recent
     minimal example.

2. Add the new value to the `DocumentSource` enum in
   `backend/danswer/configs/constants.py`. Format:
   `<SOURCE_NAME> = "<source_string>"`. The string value is what gets
   stored on every document and round-trips through Vespa — pick it
   carefully, you can't change it later without a migration that
   re-keys existing rows.

3. Register the connector in `backend/danswer/connectors/factory.py`:
   - Add the `from danswer.connectors.<source>.connector import
     <Source>Connector` import line at the top.
   - Add `DocumentSource.<SOURCE_NAME>: <Source>Connector,` to the
     `connector_map` dict in `identify_connector_class`.

**Frontend**

4. Add the new source string to the `ValidSources` union in
   `web/src/lib/types.ts`. Keep alphabetical order to make merges easier.

5. Add a `<Source>Config` interface to the same `types.ts` matching the
   shape your connector's `__init__` reads from
   `connector_specific_config`. Field names must be snake_case to match
   what the backend deserializes.

6. **If your connector uses a new credential shape**, add a
   `<Source>CredentialJson` interface to `types.ts` too. If you're
   reusing an existing credential (e.g. `GithubCredentialJson` for the
   github-files connector — both share the GitHub PAT), skip this step.

7. Add a tile entry to `web/src/lib/sources.ts` keyed by the new
   `ValidSources` string. Required: `icon`, `displayName`, `category`.
   Optional: `adminUrl` override (use it when you can't make the route
   folder match the auto-derived URL — see Critical Fact #5).

8. Create the per-source admin page at
   `web/src/app/admin/connectors/<route-segment>/page.tsx` where
   `<route-segment>` is exactly
   `displayName.toLowerCase().replaceAll(" ", "-")` (or whatever you
   put in the optional `adminUrl` override). Examples:
   `displayName: "Github"` → `connectors/github/`,
   `displayName: "GitHub-Files"` → `connectors/github-files/`,
   `displayName: "SF-Account"` → `connectors/sf-account/`.
   Clone `web/src/app/admin/connectors/github-files/page.tsx` as the
   shortest template.

**Operational**

9. **Restart every long-running process that imports the enum**:
   API server (`dapi`), background jobs (`dbe`), Slack listener
   (`dsl`). See Critical Fact #3 — a stale process will fail with
   `pydantic.ValidationError: source_type` the moment it reads back
   any indexed document of the new type from Vespa.

10. **Run the migration head check** even if you didn't write a
    migration. Adding the enum value alone doesn't need one (it's a
    Python-side `str` enum, not a DB enum), but if you also added a
    new column for connector-specific config, run `alembic upgrade head`.

### Add a backend SQL field

1. Add `Mapped[X]` column to the model in `db/models.py`. Always set
   `nullable=False` + `server_default=...` for backfill safety.
2. New Alembic migration in `backend/alembic/versions/`. Run
   `alembic heads` first to know what to set as `down_revision`.
3. If the field is exposed via API, add it to the matching Pydantic
   snapshot in `server/documents/models.py` and surface it in the
   `from_*_db_model` classmethod.
4. Mirror in `web/src/lib/types.ts`.
5. **Use this migration as both upgrade and downgrade rehearsal**:
   `alembic upgrade head && alembic downgrade -1 && alembic upgrade head`.

### Bulk-fetch a per-row computed field

This codebase has a recurring N+1 pattern in routes that loop over
cc_pairs and call helpers per row. The fix shape is:

1. Collect the lookup keys for every row up front
   (e.g. `[name_cc_cleanup_task(c, cr) for cc_pair in cc_pairs]`).
2. Add a `get_latest_*_by_*` bulk helper in `db/...` that runs *one* SQL
   query with `IN (...)` + a "max-per-group" pattern, returns a dict.
3. In the loop body, look up by key from the dict instead of calling the
   per-row helper.

See `db/tasks.py::get_latest_tasks_by_names` and the corresponding
refactor in `server/documents/connector.py::get_connector_indexing_status`
for the pattern.

### Enable Microsoft / Entra ID OIDC (local dev)

This fork hosts the OIDC plumbing in OSS — no `ee/` import required.

1. **Env vars** (see CONTRIBUTING.md → "Microsoft / Entra ID OIDC" block):
   `AUTH_TYPE=oidc`, `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`,
   `OPENID_CONFIG_URL`, `USER_AUTH_SECRET`, `WEB_DOMAIN`, and optionally
   `DEFAULT_ADMIN_EMAILS`.
2. **Run `dapi_oidc`** in the API terminal (not `dapi` — that alias hard-codes
   `AUTH_TYPE=disabled` inline, overriding any export).
3. **Entra Redirect URI**: the app registration must list
   `http://localhost:3000/auth/oidc/callback`. The Dex callback (if your tenant
   still has one registered) can stay alongside.

Files involved (don't duplicate this logic into `ee/`):

| File | What it does |
|---|---|
| `backend/danswer/main.py` (OIDC `elif` block) | Mounts `/auth/oidc/{authorize,callback}` via `httpx_oauth.clients.openid.OpenID`. |
| `backend/danswer/auth/users.py::verify_auth_setting` | Allowlist includes `AuthType.OIDC` (fork divergence vs upstream-here-only). |
| `backend/danswer/auth/users.py::oauth_callback` | After `super().oauth_callback(...)`, auto-promotes `is_verified=True` when the provider vouches for the email — works around fastapi-users not flipping the flag during the `associate_by_email` path. |
| `backend/danswer/server/auth_check.py::PUBLIC_ENDPOINT_SPECS` | `/auth/oidc/authorize` and `/auth/oidc/callback` are listed as public — `check_router_auth` raises on missing-auth routes at startup. |
| `backend/danswer/db/auth.py::SQLAlchemyUserAdminDB.create` | Role logic: if `DEFAULT_ADMIN_EMAILS` is set, only those emails become ADMIN; otherwise first-user fallback fires for bootstrap. |
| `backend/danswer/configs/app_configs.py` | `OPENID_CONFIG_URL` and `DEFAULT_ADMIN_EMAILS` parsed from env. |
| `web/src/app/auth/oidc/callback/route.ts` | Already wired; mirrors `/auth/oauth/callback/route.ts`. |
| `web/src/lib/userSS.ts` | `getAuthUrlSS` already handles `case "oidc"`. |

Trigger a fresh login flow:

```bash
# Browser is sticky on Microsoft session — incognito or sign out of Microsoft
# in another tab to force the login prompt. Otherwise Entra silent-SSO bounces
# you straight through without showing its UI.
```

### Edit credentials without re-creating the connector

Backend `PATCH /api/manage/credential/{id}` already exists. Frontend
helper is `lib/credential.ts::updateCredential`. The
`CredentialForm` component supports edit mode via the
`existingCredentialId` prop — set it and the form PATCHes instead of
POSTs. See the per-source-type setup pages for the pattern (sf-account,
sf-kbarticles, github, github-files, slack, confluence, jira, sharepoint
all have inline edit affordances). The cc-pair detail page has a
**generic** credential editor (`CredentialSection.tsx`) that works for
any connector type by introspecting `credential_json` keys.

### Bulk pause / re-enable connectors

`/admin/indexing/status` has filter + multi-select + bulk action UI.
Backend just uses the existing `PATCH /api/manage/admin/connector/{id}`
with `disabled: bool` flipped — no special bulk endpoint needed.

---

## Conventions

### Backend

- **Type hints required** — `mypy` runs in CI (`python -m mypy .` from
  `backend/`). Config lives in `backend/pyproject.toml`.
- **No `print` in non-`__main__` paths** — use `setup_logger()` from
  `danswer.utils.logger`. `print()` is fine inside `if __name__ ==
  "__main__":` blocks of connectors / scripts (there are ~58 of those
  for ad-hoc debugging) but should never appear in request handlers,
  background jobs, or library code.
- **Time**: always store timezone-aware UTC. `datetime.utcfromtimestamp`
  is deprecated in Python 3.12+ — use `datetime.fromtimestamp(s, tz=utc)`.
- **DB sessions**: never long-lived. The pattern is
  `with Session(get_sqlalchemy_engine()) as db_session: …`. Long-running
  background work eagerly loads relationships then expires the session.
- **Pre-commit**: `black` (formatter), `reorder-python-imports`,
  `autoflake` (dead-import remover). All configured in
  `.pre-commit-config.yaml`; install with `pre-commit install` from
  `backend/`. Don't fight them.

### Frontend

- **Tremor** components for admin UI (`@tremor/react`). Tailwind utility
  classes for spacing/layout.
- **Yup** for form validation, **Formik** for form state.
- **SWR** for data fetching with `refreshInterval` for live status views.
- **Heroicons / Feather** via `react-icons/fi` and the project's
  `@/components/icons/icons.tsx`.
- **TypeScript types in `lib/types.ts`** mirror Pydantic snapshots from
  `server/documents/models.py`. Out-of-sync types are the most common
  source of frontend regressions. Update both together.

### Naming

- Connector classes: `<Source>Connector` (e.g. `GithubFilesConnector`).
- Connector pydantic config: `<Source>Config` (frontend) /
  matching shape on the connector's `__init__`.
- Admin page route folders: kebab-case matching the lowercased
  `displayName`. Cross-reference with `lib/sources.ts`.
- Migration filenames: `<rev>_<short_snake_case_summary>.py`.

---

## Footguns / things to avoid

- **Don't add fields to Yup schemas if they're constants set in
  `initialValues`** — TypeScript will complain about the schema-vs-type
  shape mismatch. The pattern (used for `sf_credential_kind`) is to keep
  the constant in `initialValues` only and let Yup pass it through.
- **Don't run `alembic` from the project root** — it must be run from
  `backend/` so the `alembic.ini` resolves correctly.
- **Don't trust upstream copy-paste in connector pages** — `sfkbarticles`
  was filtering the wrong source string (`"salesforce"` instead of
  `"sfkbarticles"`) for over a year. When cloning a page, search-and-
  replace the source name carefully.
- **Don't use `echo -e`** in shell helpers — non-portable across shells.
  Use `printf`.
- **Don't `git stash` if the work-in-progress includes new files** — the
  stash doesn't include untracked files by default. Use `git stash -u`.
- **Don't drop fields from `/api/manage/admin/connector/indexing-status`**
  without checking *every* page that consumes it. See Critical Fact #4.
- **Don't pass the `priority=` kwarg to `SimpleJobClient.submit`** — only
  `dask.distributed.Client` honors it. The current code in
  `update.py::kickoff_indexing_jobs` checks `isinstance(client, Client)`
  before adding the kwarg; preserve that guard.
- **Don't reintroduce the 308 redirects in `web/next.config.js`** for
  `/api/chat/send-message`, `/api/query/stream-answer-with-quote`, or
  `/api/query/stream-query-validation`. They used to live there for stream
  proxying in older Next.js. The browser's 308 hop strips the localhost-
  scoped session cookie on the cross-origin jump to `127.0.0.1:8080`, and
  cookie-based auth (OIDC, basic, anything) breaks. Use the generic
  `/api/:path*` rewrite — Next.js 14's rewrite proxy handles streaming.
- **Don't unpin `bcrypt==4.0.1` in `backend/requirements/default.txt`**
  without also bumping `passlib`. passlib 1.7.4 reads `bcrypt.__about__`
  for version detection, which was removed in bcrypt 4.1+. Without the
  pin, every OAuth user creation explodes with
  `ValueError: password cannot be longer than 72 bytes` from passlib's
  `detect_wrap_bug` probe during bcrypt backend init.

---

## Notable session-resolved bugs (for context)

If you see code that looks "wrong but intentional", here are some
historical fixes — useful so you don't accidentally undo them:

- **`dev_run_background_jobs.py` Celery `-A`** points at
  `ee.danswer.background.celery.celery_app` (module form). The package
  form (`ee.danswer.background.celery`) doesn't work because the
  package's `__init__.py` is empty.
- **`runConnector` in `lib/connector.ts`** sends `credential_ids`
  (snake_case). It used to send `credentialIds` (camelCase) which the
  backend silently ignored, falling back to "all credentials."
- **`sfkbarticles/page.tsx`** filters `source === "sfkbarticles"`. It used
  to filter `source === "salesforce"` (copy-paste from the salesforce
  page) which made every salesforce connector show up on both pages.
- **Salesforce credential discriminator (`sf_credential_kind`)**
  distinguishes Account vs KB-Articles credentials so the two pages don't
  share each other's. Untagged legacy credentials are accepted as
  "account" type for back-compat.
- **`indexing_priority` on `IndexAttempt`** — per-attempt, not per-cc-pair
  or per-connector. See `update.py` for the Dask handoff.
- **`get_connector_indexing_status` is now O(1) queries** regardless of
  cc-pair count — per-row deletion-status lookups were bulk-fetched. Don't
  re-introduce per-row lookups in this endpoint.
- **OIDC sign-in stalled on 403 "User is not authenticated"** when chat
  was sent. Root cause was a 308 redirect in `web/next.config.js` that
  bounced `/api/chat/send-message` to `127.0.0.1:8080`, stripping the
  localhost-scoped session cookie. The streaming endpoints now flow
  through the generic `/api/:path*` rewrite — don't add per-endpoint
  redirects back. See Footguns.
- **`is_verified=False` after OIDC sign-in 403'd chat send** until
  `UserManager.oauth_callback` was patched to flip `is_verified=True`
  after `super().oauth_callback(...)` returns. fastapi-users 12.x only
  sets the flag for *newly-created* users, not for accounts associated
  via `associate_by_email`. Keep the post-super promotion in place.
- **First-user bootstrap admin** in `SQLAlchemyUserAdminDB.create` only
  fires when `DEFAULT_ADMIN_EMAILS` is empty. If the env var is set, the
  allowlist is strict — no fallback. Don't restore the unconditional
  `user_count == 0` path without the env-gate.

---

## When to ask the human

Default to "make the safe small change first, then ask." But these
specifically warrant pausing:

- **Schema migrations on tables with many rows** (esp. `index_attempt`,
  `task_queue_jobs`, `document`, Vespa). Indexes can be slow; migrations
  hold transactions.
- **Removing a `DocumentSource` enum value** — any historical document in
  Vespa with that source becomes unreadable.
- **Renaming an admin URL** — bookmarks break, and any external
  references / docs go stale.
- **Touching the celery `-A` arg or `dev_run_background_jobs.py`
  subprocess plumbing** — past breakage was silent (worker died, no logs)
  and took days to surface.
- **Bumping `NUM_INDEXING_WORKERS`** in prod — concurrency increase plus
  memory growth, plus more Dask scheduler work.
- **Changing default LLM provider** — UiPath gateway requires specific
  env vars and OAuth flow; default OpenAI doesn't.

---

## Useful one-liners

```bash
# Activate venv (the fix for most ImportError surprises)
source .venv/bin/activate

# Talk to Postgres without leaving the shell
docker exec -it danswer-stack-relational_db-1 psql -U postgres \
  -c "SELECT id, name, source FROM connector LIMIT 10;"

# Tail Celery worker output (it goes to stdout of dev_run_background_jobs.py)
ps aux | grep '[c]elery.*worker'      # confirm it's alive first

# Inspect the alembic head + chain
cd backend && alembic heads && alembic history --rev-range -3:HEAD

# Force a Next.js rebuild after enum / source-list changes
rm -rf web/.next && (cd web && npm run dev)
```

For everything else — running services, env vars, helper aliases — see
`CONTRIBUTING.md`.

---

## Files to read first when picking up new work

- `backend/danswer/db/models.py` — the data model; the ER graph in your
  head should come from this.
- `backend/danswer/configs/constants.py` — `DocumentSource` and other
  string enums that flow through the whole system.
- `backend/danswer/connectors/factory.py` — connector registry; tells you
  what types exist and where they live.
- `backend/danswer/server/documents/connector.py` — most admin REST
  routes are here.
- `backend/danswer/background/update.py` — the indexing scheduler. If
  you're working on indexing performance / priority / scheduling, this
  is the file.
- `web/src/lib/types.ts` — frontend's view of backend models. Diverges
  occasionally; reconciling it with `server/documents/models.py` is a
  recurring task.
- `web/src/lib/sources.ts` — connector tile registry (icon, displayName,
  category, optional adminUrl override). The actual TypeScript mirror
  of the backend `DocumentSource` enum is the `ValidSources` union in
  `web/src/lib/types.ts`. New connector types must be added to *both*
  files.

Don't try to read all of `update.py` or `connector.py` cold — they're
both large. Skim section headings, then dive into the specific function
relevant to the change.
