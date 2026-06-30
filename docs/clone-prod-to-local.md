# Clone prod into a local dev environment

`backend/scripts/clone_prod_to_local.py` populates a **local** dev setup with a
representative slice of **prod**, so you can test search, assistants, and the
auto-routed Search tab against realistic data before pushing changes.

It copies:

1. **Vespa** — the latest *N* documents (default 500) **per source**, including
   all their chunks with embeddings, ACLs, and document-set membership.
2. **Postgres** — assistants (`persona`), their `prompt`s and `document_set`s,
   and the `persona__prompt` / `persona__document_set` associations.

After a run, local retrieval + the assistant router + answering closely resemble
prod.

## Prerequisites

- Local app running with an **empty-ish Vespa** and a **migrated Postgres**
  (`alembic upgrade head`).
- `kubectl` access to the prod `darwin` namespace (the export runs inside a prod
  pod, which can reach prod Vespa + the prod DB).
- **The same embedding model locally as prod** (`intfloat/e5-base-v2`). The Vespa
  index name is model-specific (`danswer_chunk_<model>`); the script records the
  source index name on export and **aborts on import if the local index name
  differs** — copied vectors are meaningless to a different model. This fork's
  image pre-bakes `e5-base-v2`, so a default local setup matches.

## Usage

### Phase 1 — export (from prod)

```bash
POD=$(kubectl get pods -n darwin -l app=api-server \
        --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')

kubectl cp backend/scripts/clone_prod_to_local.py darwin/$POD:/tmp/clone.py
kubectl exec -n darwin $POD -- python /tmp/clone.py export --out /tmp/clone_bundle --per-source 500
kubectl cp darwin/$POD:/tmp/clone_bundle ./clone_bundle
```

The bundle is `meta.json` + `db.json` + `vespa/<source>.jsonl` (one chunk per
line).

### Phase 2 — import (locally)

Run from `backend/` with your **local** env. A single-node local Vespa serves the
document API on the same port as the query API, so point both at it (commonly
`8081`):

```bash
cd backend
PYTHONPATH=. \
  VESPA_HOST=localhost VESPA_PORT=8081 \
  VESPA_FEED_HOST=localhost VESPA_FEED_PORT=8081 \
  python scripts/clone_prod_to_local.py import --in ../clone_bundle --make-public
```

Restart the local API server afterward if it was already running.

## Flags

| Flag | Phase | Effect |
|---|---|---|
| `--per-source N` | export | docs per source (default 500), latest by `doc_updated_at` |
| `--sources a b c` | export | limit to specific sources (default: all) |
| `--vespa-only` / `--db-only` | both | copy only one half |
| `--make-public` | import | rewrite every chunk's ACL to `PUBLIC` so local users see everything — **local dev only** |

Sources (lowercase, as stored in Vespa): `confluence`, `slack`, `web`,
`salesforce`, `jira`, `outsystems`, `sfkbarticles`, `highspot`, `github_files`,
`file`.

## What it does NOT copy (and why that's fine)

- **Connectors / credentials / cc-pairs.** Doc-set scoping at query time uses the
  doc-set *name*, and the copied chunks already carry their `document_sets`
  membership — so a bare `document_set` row is enough for search filtering. The
  admin "Document Sets" page will show 0 connectors for imported sets; that's
  expected.
- **Persona ↔ user / user-group grants.** Personas import with `user_id=NULL` and
  `is_public=true` so your local admin sees them.

## Caveats

- **ACLs:** without `--make-public`, chunks keep their prod ACLs, so docs scoped
  to specific prod users/groups won't surface for your local user. Use
  `--make-public` for friction-free local testing. **Never** point an import with
  `--make-public` at a shared/prod Vespa.
- **Re-runnable:** DB rows upsert by primary key; Vespa chunks are PUT
  (idempotent). Safe to run repeatedly.
- **Cost/size:** chunky sources (e.g. Confluence) have many chunks per document,
  so 500 docs can be a lot of chunks. Use `--per-source` / `--sources` to trim.
