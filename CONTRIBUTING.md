<!-- DANSWER_METADATA={"link": "https://github.com/danswer-ai/danswer/blob/main/CONTRIBUTING.md"} -->

# Contributing to Danswer
Hey there! We are so excited that you're interested in Danswer.

As an open source project in a rapidly changing space, we welcome all contributions.


## 💃 Guidelines
### Contribution Opportunities
The [GitHub Issues](https://github.com/danswer-ai/danswer/issues) page is a great place to start for contribution ideas.

Issues that have been explicitly approved by the maintainers (aligned with the direction of the project)
will be marked with the `approved by maintainers` label.
Issues marked `good first issue` are an especially great place to start.

**Connectors** to other tools are another great place to contribute. For details on how, refer to this
[README.md](https://github.com/danswer-ai/danswer/blob/main/backend/danswer/connectors/README.md).

If you have a new/different contribution in mind, we'd love to hear about it!
Your input is vital to making sure that Danswer moves in the right direction.
Before starting on implementation, please raise a GitHub issue.

And always feel free to message us (Chris Weaver / Yuhong Sun) on 
[Slack](https://join.slack.com/t/danswer/shared_invite/zt-2afut44lv-Rw3kSWu6_OmdAXRpCv80DQ) / 
[Discord](https://discord.gg/TDJ59cGV2X) directly about anything at all. 


### Contributing Code
To contribute to this project, please follow the
["fork and pull request"](https://docs.github.com/en/get-started/quickstart/contributing-to-projects) workflow.
When opening a pull request, mention related issues and feel free to tag relevant maintainers.

Before creating a pull request please make sure that the new changes conform to the formatting and linting requirements.
See the [Formatting and Linting](#-formatting-and-linting) section for how to run these checks locally.


### Getting Help 🙋
Our goal is to make contributing as easy as possible. If you run into any issues please don't hesitate to reach out.
That way we can help future contributors and users can avoid the same issue.

We also have support channels and generally interesting discussions on our
[Slack](https://join.slack.com/t/danswer/shared_invite/zt-2afut44lv-Rw3kSWu6_OmdAXRpCv80DQ)
and 
[Discord](https://discord.gg/TDJ59cGV2X).

We would love to see you there!


## Get Started 🚀
Danswer being a fully functional app, relies on some external pieces of software, specifically:
- [Postgres](https://www.postgresql.org/) (Relational DB)
- [Vespa](https://vespa.ai/) (Vector DB/Search Engine)

This guide provides instructions to set up the Danswer specific services outside of Docker because it's easier for
development purposes but also feel free to just use the containers and update with local changes by providing the
`--build` flag.


### Local Set Up
It is recommended to use Python version 3.11

If using a lower version, modifications will have to be made to the code.
If using a higher version, the version of Tensorflow we use may not be available for your platform.


#### Installing Requirements
Currently, we use pip and recommend creating a virtual environment.

For convenience here's a command for it:
```bash
python -m venv .venv
source .venv/bin/activate
```

--> Note that this virtual environment MUST NOT be set up WITHIN the danswer
directory

_For Windows, activate the virtual environment using Command Prompt:_
```bash
.venv\Scripts\activate
```
If using PowerShell, the command slightly differs:
```powershell
.venv\Scripts\Activate.ps1
```

Install the required python dependencies:
```bash
pip install -r danswer/backend/requirements/default.txt
pip install -r danswer/backend/requirements/dev.txt
pip install -r danswer/backend/requirements/model_server.txt
```

Install [Node.js and npm](https://docs.npmjs.com/downloading-and-installing-node-js-and-npm) for the frontend.
Once the above is done, navigate to `danswer/web` run:
```bash
npm i
```

Install Playwright (required by the Web Connector)

> Note: If you have just done the pip install, open a new terminal and source the python virtual-env again.
This will update the path to include playwright

Then install Playwright by running:
```bash
playwright install
```


#### Dependent Docker Containers
First navigate to `danswer/deployment/docker_compose`, then start Postgres.

The simplest path is the compose-managed pair (uses a named docker volume for
Vespa's data; data lives until you `docker volume rm`):

```bash
docker compose -f docker-compose.dev.yml -p danswer-stack up -d relational_db
```

If you'd rather pin Vespa's data + logs to host-mounted directories so you
can inspect them outside Docker (and survive `docker compose down -v`),
start Postgres via compose and Vespa via a manual `docker run` on the same
network. Pick any host paths you like:

```bash
docker compose -f docker-compose.dev.yml -p danswer-stack up -d relational_db

export VESPA_VAR_STORAGE="${HOME}/danswer-vespa-data/var"
export VESPA_LOG_STORAGE="${HOME}/danswer-vespa-data/logs"
mkdir -p "$VESPA_VAR_STORAGE" "$VESPA_LOG_STORAGE"

docker run \
  --network=danswer-stack_default \
  --detach \
  --name vespa \
  --hostname index \
  --volume "$VESPA_VAR_STORAGE":/opt/vespa/var \
  --volume "$VESPA_LOG_STORAGE":/opt/vespa/logs \
  --publish 8081:8081 \
  --publish 19071:19071 \
  vespaengine/vespa:8.277.17

# Sanity check: both containers should be on the danswer-stack_default network
docker ps --format '{{ .ID }} {{ .Names }} {{ json .Networks }}'
```

(index refers to Vespa and relational_db refers to Postgres. The hostname
`index` matters — Danswer reaches Vespa by that DNS name on the shared
network.)

#### Running Danswer
To start the frontend, navigate to `danswer/web` and run:
```bash
npm run dev
```

Next, start the model server which runs the local NLP models.
Navigate to `danswer/backend` and run:
```bash
uvicorn model_server.main:app --reload --port 9000
```
_For Windows (for compatibility with both PowerShell and Command Prompt):_
```bash
powershell -Command "
    uvicorn model_server.main:app --reload --port 9000
"
```

The first time running Danswer, you will need to run the DB migrations for Postgres.
After the first time, run this any time you pull new code that touches `db/models.py`
or adds a file under `backend/alembic/versions/`.

Navigate to `danswer/backend` and with the venv active, run:
```bash
alembic upgrade head
# Verify there's a single head revision after upgrade
alembic heads
```

If `alembic heads` reports more than one head, two branches landed in parallel
and you'll need to merge them with `alembic merge -m "merge heads" <rev_a> <rev_b>`
before any new migrations can apply.

Next, start the task queue which orchestrates the background jobs.
Jobs that take more time are run async from the API server.

Still in `danswer/backend`, run:
```bash
python ./scripts/dev_run_background_jobs.py
```

To run the backend API server, navigate back to `danswer/backend` and run:
```bash
AUTH_TYPE=disabled uvicorn danswer.main:app --reload --port 8080
```
_For Windows (for compatibility with both PowerShell and Command Prompt):_
```bash
powershell -Command "
    $env:AUTH_TYPE='disabled'
    uvicorn danswer.main:app --reload --port 8080 
"
```

Note: if you need finer logging, add the additional environment variable `LOG_LEVEL=DEBUG` to the relevant services.

#### Running the Slack bot listener (optional)
If you're working on the Slack bot, run the listener as a separate process
from the project root (so `PYTHONPATH=$(pwd)` resolves the package layout):

```bash
cd backend
PYTHONPATH=$(pwd) python danswer/danswerbot/slack/listener.py
```

The listener imports the same `DocumentSource` enum the indexer uses, so if
you add a new `DocumentSource` value (e.g. when porting a new connector),
restart this process — long-running consumers won't pick up enum additions
until they're reimported, and a stale process will fail with a Pydantic
`ValidationError` on `source_type` whenever Vespa returns docs of the new
source.

#### Helper Shell Aliases (optional)
The five processes above (frontend, model server, API server, background
jobs, slack listener) each need their own terminal. Dropping the snippet
below into your `~/.zshrc` (or `~/.bashrc`) gives you one short command per
process — each one cd's into the repo, activates the venv, sets the
terminal tab title, and starts the right service.

Set `DANSWER_HOME` to your local clone of the repo before sourcing:

```bash
export DANSWER_HOME="${DANSWER_HOME:-$HOME/code/danswer}"

# Frontend (Next.js dev server, port 3000)
alias dfe='printf "\033]0;Danswer-FrontEnd\007"; cd "$DANSWER_HOME/web" && npm run dev'

# Helper used by the rest: cd to repo + source venv. Fails loudly if
# either is missing so we don't accidentally run the wrong Python.
_danswer_activate() {
  cd "$DANSWER_HOME" || return 1
  if [ ! -f ".venv/bin/activate" ]; then
    echo "Error: $DANSWER_HOME/.venv not found." >&2
    return 1
  fi
  source .venv/bin/activate
}

# Model server (local NLP models, port 9000)
dmo() {
  printf "\033]0;Danswer-Model\007"
  _danswer_activate || return 1
  cd backend && uvicorn model_server.main:app --reload --port 9000
}

# API server (FastAPI, port 8080, auth disabled for dev)
dapi() {
  printf "\033]0;Danswer-APIServer\007"
  _danswer_activate || return 1
  cd backend && AUTH_TYPE=disabled uvicorn danswer.main:app --reload --port 8080
}

# API server with Microsoft / Entra ID OIDC auth (port 8080). Needs the
# OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET / OPENID_CONFIG_URL / USER_AUTH_SECRET
# vars from the "Microsoft / Entra ID OIDC" block below. Uses your shell's
# AUTH_TYPE rather than hard-coding `disabled`.
dapi_oidc() {
  printf "\033]0;Danswer-APIServer-OIDC\007"
  _danswer_activate || return 1
  cd backend && AUTH_TYPE=oidc uvicorn danswer.main:app --reload --port 8080
}

# Background jobs (indexing loop + Celery worker + Celery beat)
dbe() {
  printf "\033]0;Danswer-Backend\007"
  _danswer_activate || return 1
  cd backend && python ./scripts/dev_run_background_jobs.py
}

# Slack listener (only if you're working on the Slack bot)
dsl() {
  printf "\033]0;Danswer-Slack-Listener\007"
  _danswer_activate || return 1
  cd backend && PYTHONPATH=$(pwd) python danswer/danswerbot/slack/listener.py
}
```

Source the file (`source ~/.zshrc`) and you can spin up the whole local
stack as `dfe`, `dmo`, `dapi`, `dbe`, `dsl` — one per terminal tab. The
`\033]0;...\007` escape sets the terminal/tab title so it's easier to find
the right window when juggling five.

#### Environment Variables
The local stack reads a fair number of environment variables across its
processes (model server, API, background jobs, frontend). The block below
is a working set for a UiPath-internal dev setup — drop it into your
`~/.zshrc` (or a sourced `.envrc`), fill in the placeholders, and source
the file.

> **Anything in `<angle-brackets>` is a placeholder.** Generate / fetch a
> real value from the relevant provider — never commit a real secret.

```bash
# ---------------------------------------------------------------------------
# Local stack toggles
# ---------------------------------------------------------------------------
export ENVIRONMENT=LOCAL
export LOG_LEVEL=info               # use `debug` if you need verbose tracing
export IMAGE_TAG=latest
export APPLY_MIGRATIONS=true
export DASK_JOB_CLIENT_ENABLED=true
export NUM_INDEXING_WORKERS=1       # see "Scaling indexing concurrency" below
export CONTINUE_ON_CONNECTOR_FAILURE=true

# ---------------------------------------------------------------------------
# Service hosts / ports — match the local docker-compose stack
# ---------------------------------------------------------------------------
export VESPA_HOST=localhost
export VESPA_PORT=8081
export VESPA_FEED_HOST=localhost
export VESPA_FEED_PORT=8081
export VESPA_CONFIG_SERVER_HOST=localhost
export MODEL_SERVER_HOST=localhost
export MODEL_SERVER_PORT=9000
export INDEXING_MODEL_SERVER_HOST=localhost
export INDEXING_MODEL_SERVER_PORT=9000
export REDIS_HOST=cache             # matches the compose service name

# Cross-encoder reranking, available locally. The model server (`dmo`) loads the
# reranker IN-PROCESS (sentence-transformers, CPU) — no extra container. Uses the
# small default model (mxbai-rerank-xsmall-v1); set RERANK_MODEL_NAME to try a
# bigger one. Reranking still only runs for assistants / chats that opt in.
# (Prod serves the reranker via a TEI container instead — see k8s/optional/tei-rerank.)
export RERANK_ENABLED=true
export LLM_RELEVANCE_FILTER_ENABLED=true   # LLM relevance filter; independent of rerank
# Advanced: to mirror prod and offload the reranker to a local TEI container
# instead of in-process, run TEI yourself and set:
# export RERANK_SERVER_URL=http://localhost:8086

# ---------------------------------------------------------------------------
# LLM (Generative AI) — UiPath LLM Gateway via OAuth client credentials
# Replace with your own gateway / model-provider settings if different.
# ---------------------------------------------------------------------------
export GEN_AI_MODEL_PROVIDER=custom
export GEN_AI_API_ENDPOINT='https://<llm-gateway-host>/llmgateway_/openai/deployments/<model-deployment>/chat/completions?api-version=<api-version>'
export GEN_AI_IDENTITY_ENDPOINT='https://<identity-host>/identity_/connect/token'
export GEN_AI_CLIENT_ID='<your-llm-client-id>'
export GEN_AI_CLIENT_SECRET='<your-llm-client-secret>'
# Leave the following empty when using GEN_AI_MODEL_PROVIDER=custom; they're
# only meaningful for direct OpenAI / Azure / Bedrock providers.
export GEN_AI_API_KEY=
export GEN_AI_MODEL_VERSION=
export GEN_AI_MAX_TOKENS=
export GEN_AI_LLM_PROVIDER_TYPE=
export FAST_GEN_AI_MODEL_VERSION=

# LLM behavior knobs (saves tokens / time during dev)
export DISABLE_LLM_FILTER_EXTRACTION=true
export DISABLE_LLM_CHOOSE_SEARCH=true
export DISABLE_LLM_CHUNK_FILTER=true
export LOG_ALL_MODEL_INTERACTIONS=true

# ---------------------------------------------------------------------------
# DB retention windows (all in days). The daily Celery beat task
# `run_retention_policies_task` runs at 08:00 UTC and deletes rows older
# than the windows below. Set any of these to 0 to disable that policy.
# See backend/danswer/db/retention.py for the exact SQL each window uses.
# Run once-off (e.g. against accumulated bloat) via:
#     cd backend && python scripts/cleanup_stale_db.py --dry-run
# ---------------------------------------------------------------------------
export RETENTION_DAYS_KOMBU=7                    # Celery broker queue (kombu_message)
export RETENTION_DAYS_TASK_QUEUE=30              # task_queue_jobs (terminal rows only)
# index_attempt is retained indefinitely by default. Connector debug
# history is generally worth keeping. To enable pruning, set this to a
# positive integer (and tune KEEP_LAST_N to keep recent attempts even
# when older than the day window):
# export RETENTION_DAYS_INDEX_ATTEMPT=60
# export RETENTION_KEEP_LAST_N_INDEX_ATTEMPTS=20
export RETENTION_DAYS_CHAT=30                    # chat_session + chat_message (cascade)
export RETENTION_DAYS_PERMISSION_SYNC=30         # permission_sync_run (terminal rows only)
export RETENTION_DAYS_USAGE_REPORTS=90           # usage_reports + file_store rows + LO blobs

# Batching knobs — DELETEs run as `WHERE id IN (SELECT id ... LIMIT N)`
# in a loop, committing per batch so table locks don't stall Celery
# enqueue/consume. Defaults handle ~1M rows per policy per daily run;
# raise MAX_BATCHES for first-time sweeps against very large bloat.
# After any policy that deletes >= one full batch, the executor runs
# `ANALYZE <table>` to refresh planner stats.
export RETENTION_BATCH_SIZE=5000                 # rows per DELETE statement
export RETENTION_MAX_BATCHES=200                 # safety ceiling per policy per run

# ---------------------------------------------------------------------------
# Analytics rollup — pre-aggregates the admin /analytics page metrics into
# `analytics_daily_rollup` so the dashboard survives chat retention deletes.
# A daily Celery beat task runs at 07:30 UTC (30 min before the retention
# sweep at 08:00 UTC). The endpoints under /api/analytics/admin/{query,user,
# danswerbot} read from this rollup.
#
# How the daily task picks its window: it reads a checkpoint
# `last_rolled_up_to` from `key_value_store` (under key
# `analytics_rollup_state`) and recomputes from
# `(checkpoint - ANALYTICS_LATE_FEEDBACK_BUFFER_DAYS)` through today,
# then advances the checkpoint. So the buffer is just the late-feedback
# grace period (most reactions arrive in minutes, but a "resolved" mark
# from a Slack helper can take a day or two), NOT a fixed window — the
# task self-heals across outages by picking up from where it left off.
#
# Safety: the task refuses to scan further back than
# `(today - (RETENTION_DAYS_CHAT - 2))`. Catastrophic outages leave older
# days at their last-known values rather than zeroing them out from
# already-deleted chat data.
#
# Deployment notes (one-time, after pulling the rollup feature):
#   1. alembic upgrade head        # creates analytics_daily_rollup
#   2. python scripts/backfill_analytics_rollup.py    # populates from history
#   3. Restart background-deployment so the new beat task is picked up
# Step (2) also seeds the checkpoint to its --end date (today by default),
# so the daily task starts from the right place.
# ---------------------------------------------------------------------------
export ANALYTICS_LATE_FEEDBACK_BUFFER_DAYS=2     # late-feedback grace period

# ---------------------------------------------------------------------------
# Microsoft / Entra ID OIDC (optional — only when running `dapi_oidc` instead
# of `dapi`). Skip this whole block for the default `AUTH_TYPE=disabled` flow.
#
# 1. In Entra portal → App registrations, register an app (or reuse the
#    existing tenant one) and add `http://localhost:3000/auth/oidc/callback`
#    to its Redirect URIs. Note the Application (client) ID, generate a
#    client secret, and grab the Directory (tenant) ID.
# 2. Fill the values below and re-source.
# 3. `dapi_oidc` (instead of `dapi`) in the API terminal. `dfe` stays the
#    same — the frontend reads AUTH_TYPE dynamically from /auth/type.
# 4. Hit http://localhost:3000/auth/login → bounces through Microsoft and
#    issues a session cookie scoped to localhost:3000.
#
# DEFAULT_ADMIN_EMAILS: comma-separated emails that land as ADMIN on first
# sign-in. Leave empty to fall back to the "first user wins" bootstrap.
# Set in any environment where you don't want the first signer to be admin.
# ---------------------------------------------------------------------------
export OAUTH_CLIENT_ID='<entra-application-client-id>'
export OAUTH_CLIENT_SECRET='<entra-client-secret>'
export OPENID_CONFIG_URL='https://login.microsoftonline.com/<entra-tenant-id>/v2.0/.well-known/openid-configuration'
export USER_AUTH_SECRET="$(openssl rand -hex 32)"   # any long random string; must stay stable across restarts
export WEB_DOMAIN='http://localhost:3000'
export DEFAULT_ADMIN_EMAILS='user1@uipath.com,user2@uipath.com'

# ---------------------------------------------------------------------------
# GitHub PAT — used by the `gh` CLI and the GitHub / GitHub-Files connectors
# ---------------------------------------------------------------------------
export GITHUB_TOKEN='<your-github-personal-access-token>'

# ---------------------------------------------------------------------------
# Salesforce — only needed when running the Salesforce connector __main__
# blocks or the helper scripts under backend/scripts/ (e.g.
# preview_salesforce_accounts.py). Use single quotes around the password
# to preserve `$`, `<`, `*`, etc. Wrap with a leading space to keep the
# secret out of shell history (zsh: HIST_IGNORE_SPACE).
# ---------------------------------------------------------------------------
export SF_CLIENT_ID='<your-salesforce-connected-app-client-id>'
export SF_CLIENT_SECRET='<your-salesforce-connected-app-client-secret>'
export SF_USERNAME='<your-salesforce-username>'
export SF_PASSWORD='<your-salesforce-password>'
# export SF_LOGIN_URL='https://test.salesforce.com'   # uncomment for sandbox

# ---------------------------------------------------------------------------
# Slack bot — only needed when running danswerbot/slack/listener.py
# ---------------------------------------------------------------------------
export DANSWER_BOT_SLACK_APP_TOKEN='<your-slack-app-level-token>'   # xapp-…
export DANSWER_BOT_SLACK_BOT_TOKEN='<your-slack-bot-token>'         # xoxb-…

# ---------------------------------------------------------------------------
# macOS-only PATH addition for Docker Desktop's CLI shims
# ---------------------------------------------------------------------------
if [ -d "/Applications/Docker.app/Contents/Resources/bin" ]; then
  export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
fi
```

A few things worth knowing about this set:

- **`PYTHONPATH` is *not* exported globally** here. The shell aliases above
  set it per-command (`PYTHONPATH=$(pwd)` from inside `backend/`), which
  resolves the package layout correctly without polluting other Python
  invocations.
- **`NUM_INDEXING_WORKERS=1`** is the default. Each indexing worker is one
  Dask process; with a single worker, only one indexing attempt runs at a
  time. Bump to 2–4 if you want higher-priority attempts to overlap with
  in-flight ones rather than wait for them to finish.
- **`DISABLE_LLM_*` flags** short-circuit several optional LLM calls in the
  search pipeline. Useful in dev to keep iteration fast and avoid spending
  tokens; turn them off (or unset) if you're testing those code paths.
- **Slack tokens are workspace-specific.** You won't pick up another team's
  bot token by accident — but never paste them into a PR or Slack thread.
- **Salesforce credentials**: only needed when running the connector
  directly (`python danswer/connectors/salesforce/connector.py`) or the
  preview / dump scripts. The runtime indexer reads credentials from the
  database, not the environment.

#### Scaling Indexing Concurrency
By default `NUM_INDEXING_WORKERS=1` — the indexer process spawns a single
Dask worker, so only one indexing attempt runs at a time. Bump this when
you have many connectors and the queue keeps growing. The runtime guards
against the most dangerous race (two attempts for the same cc-pair
running concurrently) with two layers, both of which leave the attempt
in `NOT_STARTED` rather than marking it FAILED:

1. **Scheduler-side defer** (`update.py::kickoff_indexing_jobs`) — before
   submitting a NOT_STARTED attempt to Dask, check whether another
   attempt for the same `(connector, credential, embedding_model)` is
   already IN_PROGRESS. If yes, skip the submission this tick.
2. **Worker-side advisory lock** (`run_indexing_entrypoint` +
   `try_acquire_cc_pair_lock`) — true-race safety net for the case where
   two NOT_STARTED rows for the same cc-pair are submitted to two
   workers in the same scheduler tick. If the lock fails, the worker
   reverts the attempt back to `NOT_STARTED` (clears `time_started`) so
   the next scheduler tick picks it up again. **No FAILED row is
   produced.** The previous behaviour wrote `skipped_concurrent_cc_pair_run`
   FAILED rows; this no longer happens.

Three downstream things to scale alongside it:

1. **Model server**: by default it runs as a single uvicorn process. Four
   indexing workers calling `/encoder` simultaneously will queue. Run it
   with workers:
   ```bash
   uvicorn model_server.main:app --port 9000 --workers 4
   ```
   (or set the worker count to roughly `NUM_INDEXING_WORKERS`).

2. **Postgres `max_connections`**: each Dask worker is a subprocess with
   its own SQLAlchemy connection pool (default 5 + 10 overflow). At
   `NUM_INDEXING_WORKERS=4` the indexer process group alone may reach
   ~75 connections; add the API server, Celery worker, and beat
   scheduler and you can hit the Postgres default of `100`. Either lower
   the per-process pool size or raise the Postgres limit:
   ```sql
   ALTER SYSTEM SET max_connections = 200;  -- requires DB restart
   ```

3. **Source-side rate limits**: 4 concurrent GitHub workers means PAT
   rate limits hit 4× faster, same for Salesforce / Confluence / Jira
   API quotas. The runtime caps per-source-group concurrency
   independently of `NUM_INDEXING_WORKERS` — see the next section. So
   even with `NUM_INDEXING_WORKERS=4`, GitHub's cap stays at 1 by
   default, four GitHub repos still serialize, and the other workers
   are free to run other sources.

Both queueing decisions (per-cc-pair collision + per-source cap) leave
attempts as `NOT_STARTED` — neither produces FAILED rows. So the
indexing-status table never accumulates "skipped" failure rows for
routine deferral. Only real indexing errors show up as FAILED.

#### Per-Source Indexing Concurrency Cap
Even with `NUM_INDEXING_WORKERS > 1`, you typically don't want N parallel
indexers all hammering the same external API — most sources rate-limit
per credential token. The generic rule the runtime enforces:

> **At most `INDEXING_PER_SOURCE_CAP` indexing attempts per
> `DocumentSource` are submitted to Dask concurrently.** Default is `1`.

So `NUM_INDEXING_WORKERS=4` + 4 different source types (Slack + GitHub +
Confluence + Jira) gives you a 4× speedup. Same `NUM_INDEXING_WORKERS=4`
+ 4 GitHub cc-pairs (same source) → 1 runs and the other 3 stay in
`NOT_STARTED`. The scheduler reconsiders them on each tick (every 10s);
the moment the running one finishes, the next NOT_STARTED row for that
source gets submitted.

Adding a new connector requires no config change — every `DocumentSource`
gets its own slot pool automatically. Connectors that happen to share an
external credential token (e.g. `github` and `github_files` both use a
PAT) are *not* collapsed into a single bucket; if that distinction
matters for your rate limits, fold them into a single `DocumentSource`
rather than re-introducing a grouping layer.

To disable the cap entirely (e.g. you genuinely have separate
credentials per cc-pair and want them parallel):

```bash
export INDEXING_PER_SOURCE_CAP=0
```

`0` = uncapped (only `NUM_INDEXING_WORKERS` and the per-cc-pair lock
constrain you). Default is `1`. Higher integers also work — `2` would
let two concurrent attempts per source type, etc.

The cap is enforced in the scheduler
(`background/update.py::kickoff_indexing_jobs`). Before submitting each
NOT_STARTED attempt to Dask, the scheduler counts IN_PROGRESS attempts
for the same source; if that count is already at the cap, the attempt
is left as NOT_STARTED and reconsidered on the next tick. There are no
FAILED rows produced by capping — only a `Deferring indexing attempt
{id} ... cap of N reached` log line.

#### Common Pitfalls
- **Stuck `cleanup_connector_credential_pair_*` task in `task_queue_jobs`**.
  Almost always means the Celery worker spawned by `dev_run_background_jobs.py`
  died at startup — the script's `subprocess.Popen` doesn't propagate child
  exit codes, so worker boot errors only show up in the per-line WORKER:
  output. Look for `Error: Invalid value for '-A' / '--app'` or `'TaskPool'
  object has no attribute 'grow'`. Fix whatever's wrong, then restart the
  script and the queued tasks drain in seconds.
- **Vespa unreachable on `index:8081`**. The backend resolves Vespa by the
  DNS name `index` on the `danswer-stack_default` docker network. If you
  ran Vespa via `docker run` (above), make sure both `--network` and
  `--hostname` match. `docker network inspect danswer-stack_default` will
  show whether Vespa is attached.
- **Frontend doesn't reflect a new connector tile or label**. Next.js's
  `.next/cache` can hold stale module bundles after enum / source-list
  edits. `rm -rf web/.next && (cd web && npm run dev)` from the repo root,
  then hard-refresh.

### Testing

See [`TESTING.md`](./TESTING.md) for the full testing reference: the
four orchestrator scripts under `backend/scripts/` (`test_analytics_e2e.py`,
`test_features_e2e.py`, `test_celery_jobs_smoke.py`, `seed_test_data.py`),
their assertion checklists, manual UI smoke steps, stress-test profiles,
the seed-script knob reference, and troubleshooting.

Quickest path to confidence — assumes a dev DB:

```bash
cd backend
PYTHONPATH=$(pwd) python scripts/test_analytics_e2e.py --yes   # ~30s, full pipeline
PYTHONPATH=$(pwd) python scripts/test_features_e2e.py --yes    # ~5s, feature regressions
PYTHONPATH=$(pwd) python scripts/test_celery_jobs_smoke.py --yes  # ~10s, broker→worker
```

### Formatting and Linting
#### Backend
For the backend, you'll need to setup pre-commit hooks (black / reorder-python-imports).
First, install pre-commit (if you don't have it already) following the instructions
[here](https://pre-commit.com/#installation).
Then, from the `danswer/backend` directory, run:
```bash
pre-commit install
```

Additionally, we use `mypy` for static type checking.
Danswer is fully type-annotated, and we would like to keep it that way! 
To run the mypy checks manually, run `python -m mypy .` from the `danswer/backend` directory.


#### Web
We use `prettier` for formatting. The desired version (2.8.8) will be installed via a `npm i` from the `danswer/web` directory. 
To run the formatter, use `npx prettier --write .` from the `danswer/web` directory.
Please double check that prettier passes before creating a pull request.


### Release Process
Danswer follows the semver versioning standard.
A set of Docker containers will be pushed automatically to DockerHub with every tag.
You can see the containers [here](https://hub.docker.com/search?q=danswer%2F).
