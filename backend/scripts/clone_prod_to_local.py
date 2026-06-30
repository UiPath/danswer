"""Clone a representative slice of PROD into a LOCAL dev environment.

Copies, into a local setup that already runs the app + an empty Vespa + a
migrated Postgres:

  1. Vespa: the latest N documents (default 500) PER SOURCE — all their chunks,
     including embeddings, ACLs and document-set membership.
  2. Postgres: assistants (personas) + their prompts + document sets + the
     persona<->prompt and persona<->document_set associations.

so local retrieval + the assistant router + answering closely resemble prod and
you can test before pushing.

==============================================================================
HOW TO RUN  (two phases, connected by a bundle directory)
==============================================================================
Phase 1 — EXPORT (run INSIDE a prod pod, which can reach prod Vespa + prod DB):

    POD=$(kubectl get pods -n darwin -l app=api-server \
            --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
    kubectl cp backend/scripts/clone_prod_to_local.py darwin/$POD:/tmp/clone.py
    kubectl exec -n darwin $POD -- python /tmp/clone.py export --out /tmp/clone_bundle --per-source 500
    kubectl cp darwin/$POD:/tmp/clone_bundle ./clone_bundle

Phase 2 — IMPORT (run LOCALLY, with your LOCAL env: VESPA_HOST=localhost,
local POSTGRES_*; from the backend/ dir so `danswer` imports):

    cd backend
    PYTHONPATH=. python scripts/clone_prod_to_local.py import --in ../clone_bundle --make-public

==============================================================================
IMPORTANT CONSTRAINTS / CAVEATS
==============================================================================
* EMBEDDING MODEL must match. The Vespa index name is model-specific
  (danswer_chunk_<model>). Export records the source index name; import refuses
  if the local index name differs (different model => copied vectors are
  meaningless). Prod = intfloat/e5-base-v2; this fork's image pre-bakes it, so a
  default local setup matches.
* ACLs: chunks keep their prod access_control_list. Local users won't match
  private (user/group) ACLs, so those docs won't surface locally. Pass
  --make-public on import to rewrite every chunk's ACL to PUBLIC — LOCAL DEV
  ONLY; never point import at a shared/prod Vespa with that flag.
* Connectors/credentials are NOT cloned. Doc-set scoping at query time uses the
  doc-set NAME (the chunks already carry their document_sets membership), so a
  bare document_set row (no connectors) is enough for search filtering. The
  admin "Document Sets" page will show 0 connectors for them — expected.
* Personas are imported with user_id=NULL and is_public=true so your local admin
  sees them; persona<->user / persona<->user_group grants are NOT copied.
* Re-runnable: DB rows upsert by primary key; Vespa chunks PUT (idempotent).
"""
import argparse
import gzip
import json
import os
import sys
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

import httpx
from sqlalchemy import MetaData
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from danswer.configs.app_configs import VESPA_FEED_HOST
from danswer.configs.app_configs import VESPA_FEED_PORT
from danswer.configs.app_configs import VESPA_HOST
from danswer.configs.app_configs import VESPA_PORT
from danswer.db.embedding_model import get_current_db_embedding_model
from danswer.db.engine import get_sqlalchemy_engine

# Same container URLs the app builds (search -> query container, document/visit
# -> feed container).
VESPA_APP_CONTAINER_URL = f"http://{VESPA_HOST}:{VESPA_PORT}"
VESPA_FEED_CONTAINER_URL = f"http://{VESPA_FEED_HOST}:{VESPA_FEED_PORT}"


# Lowercase source_type values as stored in Vespa (confirmed against prod).
DEFAULT_SOURCES = [
    "confluence",
    "slack",
    "web",
    "salesforce",
    "jira",
    "outsystems",
    "sfkbarticles",
    "highspot",
    "github_files",
    "file",
]

# DB tables cloned, in FK-safe insert order. (joins last.)
DB_TABLES = [
    "prompt",
    "document_set",
    "persona",
    "persona__prompt",
    "persona__document_set",
]

DOC_ID_BATCH = 15  # doc_ids per Vespa visit selection
HTTP_TIMEOUT = 60.0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _index_name() -> str:
    with Session(get_sqlalchemy_engine()) as db:
        return get_current_db_embedding_model(db).index_name


def _esc(s: str) -> str:
    """Escape a string for a Vespa selection single-quoted literal (the format
    the app uses in document_index/vespa/index.py)."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _docid_from_vespa_id(vespa_id: str) -> str:
    # "id:default:<index>::<user_specified_id>" -> "<user_specified_id>"
    return vespa_id.split("::", 1)[1]


# --------------------------------------------------------------------------- #
# EXPORT
# --------------------------------------------------------------------------- #
def _latest_doc_ids(client: httpx.Client, index: str, source: str, n: int) -> list[str]:
    """The N most-recently-updated distinct document_ids for a source."""
    yql = (
        f"select * from {index} where source_type contains @src | "
        f"all(group(document_id) max({n}) order(-max(doc_updated_at)) each())"
    )
    resp = client.post(
        f"{VESPA_APP_CONTAINER_URL}/search/",
        json={"yql": yql, "src": source, "hits": 0, "timeout": "30s"},
    )
    resp.raise_for_status()
    doc_ids: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            # group leaf: {"value": "<document_id>", "fields": {...}}
            if "value" in node and isinstance(node["value"], str) and "children" not in node:
                doc_ids.append(node["value"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    # only walk into the grouping subtree to avoid catching unrelated "value"s
    root = resp.json().get("root", {})
    walk(root.get("children", []))
    # de-dup, preserve order
    seen: set[str] = set()
    ordered = [d for d in doc_ids if not (d in seen or seen.add(d))]
    return ordered[:n]


def _iter_chunks_for_docs(
    client: httpx.Client, index: str, doc_ids: list[str]
) -> "Iterator[dict]":
    """Yield chunks for the given document_ids via the Vespa visit API.

    Streams (yields one chunk at a time) rather than accumulating — a source's
    500 docs can be many chunks each carrying a 768-float embedding, so buffering
    them all OOM-kills the exporter in a memory-limited pod.
    """
    url = f"{VESPA_FEED_CONTAINER_URL}/document/v1/default/{index}/docid"
    for i in range(0, len(doc_ids), DOC_ID_BATCH):
        batch = doc_ids[i : i + DOC_ID_BATCH]
        selection = " or ".join(f"{index}.document_id=='{_esc(d)}'" for d in batch)
        cont: str | None = None
        while True:
            params = {"selection": selection, "wantedDocumentCount": 1000}
            if cont:
                params["continuation"] = cont
            r = client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            for doc in data.get("documents", []):
                yield {"id": doc["id"], "fields": doc["fields"]}
            cont = data.get("continuation")
            if not cont:
                break


def export(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    (out_dir / "vespa").mkdir(parents=True, exist_ok=True)
    index = _index_name()
    sources = args.sources or DEFAULT_SOURCES
    print(f"[export] index_name={index}  sources={sources}  per_source={args.per_source}")

    meta = {"index_name": index, "per_source": args.per_source, "sources": sources}
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    if not args.db_only:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            for src in sources:
                doc_ids = _latest_doc_ids(client, index, src, args.per_source)
                if not doc_ids:
                    print(f"[export]   {src}: 0 documents", flush=True)
                    continue
                # Stream straight to a gzip file: bounded memory + a much smaller
                # bundle (embedding float-text compresses well).
                path = out_dir / "vespa" / f"{src}.jsonl.gz"
                n = 0
                with gzip.open(path, "wt") as f:
                    for c in _iter_chunks_for_docs(client, index, doc_ids):
                        f.write(json.dumps(c) + "\n")
                        n += 1
                print(
                    f"[export]   {src}: {len(doc_ids)} docs, {n} chunks -> {path.name}",
                    flush=True,
                )

    if not args.vespa_only:
        db_dump: dict[str, list[dict]] = {}
        with Session(get_sqlalchemy_engine()) as db:
            for tbl in DB_TABLES:
                rows = [dict(r._mapping) for r in db.execute(text(f"SELECT * FROM {tbl}"))]
                db_dump[tbl] = rows
                print(f"[export]   db.{tbl}: {len(rows)} rows")
        (out_dir / "db.json").write_text(json.dumps(db_dump, indent=2, default=str))
    print(f"[export] done -> {out_dir}")


# --------------------------------------------------------------------------- #
# IMPORT
# --------------------------------------------------------------------------- #
def _import_vespa(out_dir: Path, local_index: str, make_public: bool) -> None:
    vespa_dir = out_dir / "vespa"
    files = sorted(vespa_dir.glob("*.jsonl.gz")) + sorted(vespa_dir.glob("*.jsonl"))
    if not files:
        print("[import] no vespa/*.jsonl(.gz) found, skipping Vespa")
        return
    total = 0
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        for path in files:
            n = 0
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt") as fh:
                lines = fh.readlines()
            for line in lines:
                if not line.strip():
                    continue
                rec = json.loads(line)
                fields = rec["fields"]
                if make_public:
                    fields["access_control_list"] = {"PUBLIC": 1}
                docid = _docid_from_vespa_id(rec["id"])
                quoted = urllib.parse.quote(docid, safe="")
                url = (
                    f"{VESPA_FEED_CONTAINER_URL}/document/v1/default/"
                    f"{local_index}/docid/{quoted}"
                )
                r = client.post(url, json={"fields": fields})
                r.raise_for_status()
                n += 1
            total += n
            print(f"[import]   {path.stem}: fed {n} chunks")
    print(f"[import] vespa: {total} chunks total")


def _import_db(out_dir: Path) -> None:
    db_path = out_dir / "db.json"
    if not db_path.exists():
        print("[import] no db.json found, skipping DB")
        return
    dump = json.loads(db_path.read_text())
    engine = get_sqlalchemy_engine()
    md = MetaData()
    md.reflect(bind=engine, only=DB_TABLES)

    # Per-table overrides so prod rows are usable locally (no prod users/grants).
    overrides = {
        "persona": {"user_id": None, "is_public": True},
        "document_set": {"user_id": None, "is_public": True, "is_up_to_date": True},
        "prompt": {"user_id": None},
    }

    with engine.begin() as conn:
        for tbl in DB_TABLES:  # FK-safe order
            rows = dump.get(tbl, [])
            table = md.tables[tbl]
            colnames = {c.name for c in table.columns}
            pk = [c.name for c in table.primary_key]
            for row in rows:
                row = {k: v for k, v in row.items() if k in colnames}
                row.update(overrides.get(tbl, {}))
                stmt = pg_insert(table).values(**row)
                update_cols = {c: stmt.excluded[c] for c in row if c not in pk}
                if update_cols:
                    stmt = stmt.on_conflict_do_update(index_elements=pk, set_=update_cols)
                else:
                    stmt = stmt.on_conflict_do_nothing(index_elements=pk)
                conn.execute(stmt)
            print(f"[import]   db.{tbl}: upserted {len(rows)} rows")
            # keep the id sequence ahead of the explicit ids we just inserted
            if "id" in colnames:
                conn.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('{tbl}', 'id'), "
                        f"GREATEST((SELECT COALESCE(MAX(id), 1) FROM {tbl}), 1))"
                    )
                )


def import_(args: argparse.Namespace) -> None:
    out_dir = Path(args.in_dir)
    meta = json.loads((out_dir / "meta.json").read_text())
    src_index = meta["index_name"]
    local_index = _index_name()
    if src_index != local_index:
        sys.exit(
            f"[import] ABORT: embedding-model/index mismatch.\n"
            f"  exported from: {src_index}\n  local:         {local_index}\n"
            f"  The local embedding model must match prod (intfloat/e5-base-v2) "
            f"or copied vectors are meaningless."
        )
    print(f"[import] index_name={local_index}  make_public={args.make_public}")
    if not args.db_only:
        _import_vespa(out_dir, local_index, args.make_public)
    if not args.vespa_only:
        _import_db(out_dir)
    print("[import] done. Restart the local API server if it was already running.")


# --------------------------------------------------------------------------- #
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("export", help="export from PROD (run inside a prod pod)")
    pe.add_argument("--out", required=True, help="bundle output directory")
    pe.add_argument("--per-source", type=int, default=500)
    pe.add_argument("--sources", nargs="*", help=f"defaults to: {DEFAULT_SOURCES}")
    pe.add_argument("--vespa-only", action="store_true")
    pe.add_argument("--db-only", action="store_true")
    pe.set_defaults(func=export)

    pi = sub.add_parser("import", help="import into LOCAL (run locally)")
    pi.add_argument("--in", dest="in_dir", required=True, help="bundle directory")
    pi.add_argument(
        "--make-public",
        action="store_true",
        help="rewrite every chunk ACL to PUBLIC (LOCAL DEV ONLY)",
    )
    pi.add_argument("--vespa-only", action="store_true")
    pi.add_argument("--db-only", action="store_true")
    pi.set_defaults(func=import_)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
