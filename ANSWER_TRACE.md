# Answer Trace — operator provenance for a single answer

`answer_trace` records the branch-level pipeline decisions that produced one answer
(chat or search), so an operator can reconstruct *how* an answer was arrived at
without a UI. It's DB-backed (one row per answer) because logs aren't yet centralized
across services, so the table **is** the cross-service search layer. On by default;
pruned after **14 days**.

Not for end users — that's a separate two-field badge (see "Badge" below). And it
records only **deterministic decisions actually taken**; it never reconstructs or
narrates the LLM's internal reasoning.

## How it's collected

A request-scoped `contextvars` trace (`danswer/answer_trace/tracer.py`) — no signature
threading. `start_trace(...)` opens it after the persona resolves; decision points call
`event(...)` / `record_deep_rag(...)` / `set_fields(...)` from anywhere in the call
stack; `finalize_and_persist(db_session, chat_message_id=...)` writes the row in the
same transaction as the answer message. When tracing is disabled every call is a
no-op — the hot path is unchanged.

- Gate: `ANSWER_TRACE_ENABLED` (default `true`), `ANSWER_TRACE_RETENTION_DAYS` (default `14`).
- Caveat: `contextvars` don't propagate into `SearchPipeline`'s worker threads, so v1
  captures branch-level decisions on the main request thread (routing, deep-rag stages,
  cited sources), not per-thread retrieval internals.

## Schema

One row per answer. Filterable dimensions are columns; ordered per-stage detail is in
the `events` JSONB. Events hold doc/chunk **identifiers**, never chunk content.

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `trace_uuid` | uuid, unique | universal handle (chat AND search) |
| `chat_message_id` | int FK → chat_message, unique, null | null on the search surface |
| `chat_session_id` | int, null, indexed | |
| `user_id` | uuid, null, indexed | who asked (id, not content) |
| `surface` | text | `chat` \| `search` |
| `persona_id` | int, null | assistant used |
| `created_at` | timestamptz, indexed | |
| `query` | text | raw question (user content — same ACL as chat data) |
| `rephrased_query` | text, null | |
| `route_stage` | text, null | `keyword` \| `global_rule` \| `knn` \| `default` (search) |
| `route_confidence` | float, null | |
| `retrieved_count` | int, null | candidates from Vespa |
| `reranked` | bool | |
| `cited_source_count` | int, null | distinct cited docs (the badge number) |
| `deep_rag_enabled` | bool | |
| `deep_rag_escalated` | bool | did it actually decompose |
| `sub_question_count` | int, null | |
| `model` | text, null | |
| `total_duration_ms` | int, null | |
| `trace_version` | int | schema evolution |
| `events` | jsonb | ordered `[{stage, data, duration_ms?}]` |

Model: `db/models.py::AnswerTrace`. Migration: `answer_trace_01` (chained on `deeprag0001`).

## Sample row — a chat answer where Deep RAG escalated

| column | value |
|---|---|
| trace_uuid | `9f2c1e7a-4b30-4d9a-bd2e-1c6f0a83e5d1` |
| chat_message_id | 90217 |
| chat_session_id | 5541 |
| surface | `chat` |
| persona_id | 315 |
| query | `How does elastic robot orchestration differ between Automation Cloud and Automation Suite?` |
| reranked | `false` (GPU reranker disabled cluster-wide) |
| cited_source_count | 3 |
| deep_rag_enabled / escalated | `true` / `true` |
| sub_question_count | 3 |
| model | `gpt-4o-2024-11-20` |
| total_duration_ms | 8730 |

`events`:

```json
[
  {"stage": "deep_rag.single_shot", "data": {"coverage": "partial"}},
  {"stage": "deep_rag.decompose", "data": {"sub_questions": [
      "What are the elastic robot orchestration features in Automation Cloud?",
      "What are the elastic robot orchestration features in Automation Suite?",
      "What are the key differences between the two?"]}},
  {"stage": "deep_rag.compare_guard", "data": {"kept": "multi_hop"}}
]
```

## Querying

```sql
-- full trace for one answer
SELECT events FROM answer_trace WHERE chat_message_id = 90217;
-- or by the universal handle the client received:
SELECT * FROM answer_trace WHERE trace_uuid = '9f2c1e7a-...';

-- deep-searched but cited almost nothing (suspicious answers)
SELECT trace_uuid, query, sub_question_count, cited_source_count
FROM answer_trace
WHERE deep_rag_escalated AND cited_source_count <= 1
ORDER BY created_at DESC LIMIT 50;

-- escalation rate + p50 latency by surface, last 7 days
SELECT surface,
       avg(deep_rag_escalated::int) AS escalation_rate,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY total_duration_ms) AS p50_ms
FROM answer_trace
WHERE created_at > now() - interval '7 days'
GROUP BY surface;

-- everything a user asked in a session
SELECT created_at, query, deep_rag_escalated, cited_source_count
FROM answer_trace WHERE chat_session_id = 5541 ORDER BY created_at;
```

## Retention

`prune_old_traces(db_session, days=14)` deletes rows older than N days. Run it from the
periodic cleanup job (not yet scheduled — see Future work).

## End-user badge (projection, not the trace)

The chat/search UI shows a two-field projection of the same data:
`Answered from {cited_source_count} sources`, plus `· deep-searched {sub_question_count}
angles` **only when `deep_rag_escalated` is true** (report what happened, not the toggle
state). Exposing these three fields to the client is the one net-new bit the badge needs;
it's a trivial read once the trace exists.

## Future work

- Schedule `prune_old_traces` in the periodic cleanup (celery beat).
- Expose `cited_source_count` + `deep_rag_escalated` + `sub_question_count` to the client
  for the badge.
- Instrument the remaining branch points (routing on search, retrieve/rerank/relevance)
  — currently only deep-rag stages + cited/retrieved counts are captured on the chat path.
- Deep-rag `cited_source_count` (parse `[n]` citations in the synthesized answer back to
  doc ids) — ties to the deep-rag rich-citation future work.
