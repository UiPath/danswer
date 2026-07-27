# Deep RAG (feature-gated, verdict-gated multi-hop)

Deep RAG lets an assistant escalate a hard question to a multi-step retrieval loop
(decompose → retrieve per sub-question → synthesize) when a single-shot answer is
judged incomplete. It is **off by default** and gated on two flags; when off, the
chat path is exactly as before.

## Behavior

| assistant `deep_rag_enabled` | chat request `deep_rag` | behavior |
|---|---|---|
| off | (n/a) | current single-shot; toggle not shown |
| on | off (default) | current single-shot |
| on | on | Deep RAG: single-shot **+ verdict-gated escalation + compare-guard** |

"On" means *escalation is allowed* — a simple question that single-shot fully
answers still won't decompose. Only a `partial` self-verdict escalates.

## Flow (`backend/danswer/deep_rag/controller.py`)

1. **single-shot** answer + self-reported coverage verdict, in one LLM call
   (`{answer, coverage: complete|partial, missing:[...]}`).
2. `complete` (or unparseable) → return single-shot. No escalation (~1× cost).
3. `partial` → **decompose** (cap N=3, seeded by the verdict's `missing`) →
   **retrieve per sub-question** (deduped) → **synthesize** →
   **compare-guard**: keep the deeper answer only if it is *substantially* better,
   else fall back to single-shot.
4. **Fail-open**: any error in the deep path returns the single-shot answer.

Retrieval uses `SearchPipeline` with the **real user + persona ACL** (never
`bypass_acl` — that was eval-only).

## Why this gate (the eval)

Measured on 40 Automation Suite / Orchestrator questions (see
`multihop-gate-eval-40.md`, `multihop-rag-comparison.md`):

- The **single-shot self-verdict** is the only gate with **100% recall** — it never
  silently ships the worse answer. An up-front "is this multi-hop?" classifier tops
  out ~90% recall because it decides *blind* (pre-retrieval) and misses
  "looks-simple-but-retrieves-empty" questions.
- **Retrieval score is not a usable gate signal** — top scores are ~0.79–0.94 for
  both needed and not-needed questions (no separation). Do not gate on score.
- Only ~48% of even "hard-looking" questions actually needed multi-hop, so the
  compare-guard (which makes false-positive escalations harmless) matters.

## Upstream learnings (main = Onyx, Mar-2025), NOT the graph

- Ported prompts (`backend/danswer/prompts/deep_rag.py`): `DECOMPOSITION_PROMPT`
  (from `INITIAL_QUESTION_DECOMPOSITION_PROMPT` — ≤3 sub-Qs, entity-isolation,
  RAG-phrased) and `COMPARISON_PROMPT` (from `INITIAL_REFINED_ANSWER_COMPARISON_PROMPT`
  — the compare-guard).
- Upstream's own auto-gate is a stub (`decide_refinement_need = True`,
  `validate_initial_answer = True`); it relies entirely on a user "Deep Research"
  toggle. We are ahead there with the real verdict gate, and keep the manual toggle
  as the override.
- We deliberately do **not** adopt upstream's LangGraph substrate — a plain Python
  controller over the existing `SearchPipeline` captures most of the value at a
  fraction of the LLM calls.

## Files (by phase)

- **Controller + prompts**: `backend/danswer/deep_rag/controller.py`,
  `backend/danswer/prompts/deep_rag.py`; tests in
  `backend/tests/unit/danswer/deep_rag/`.
- **Assistant flag**: `Persona.deep_rag_enabled` (migration `deeprag0001`),
  threaded through `db/persona.py` + `server/features/persona/models.py`; admin
  toggle in `web/src/app/admin/assistants/AssistantEditor.tsx`.
- **Chat override**: `CreateChatMessageRequest.deep_rag`; branch in
  `backend/danswer/chat/process_message.py` (`stream_chat_message_objects`).
- **Chat toggle**: `web/src/app/chat/{ChatPage.tsx,lib.tsx,input/ChatInputBar.tsx}`.

## Beta limitations

- The synthesized answer streams as a **single piece** (not token-by-token).
- Citations appear as **`[n]` text**; the rich doc-reference/citation UI is not
  wired yet (`reference_docs=None`, `citations=None` on persist).
- No token-usage/tool-call metadata for the deep path.
- **Conversation history IS threaded** (Phase 6) into the single-shot verdict and the
  decomposition prompts, so multi-turn follow-ups resolve in context (mirrors
  upstream). Residual: the *single-shot retrieval* still queries the raw message —
  a history-aware query rephrase before the first retrieval is a possible refinement
  (see Future work). The escalation path is fully history-aware because the
  decomposition (which drives sub-question retrieval) sees the history.

## Enable / test

1. `alembic upgrade head` (applies `deeprag0001`), bounce api-server + background.
2. Admin → edit an assistant → enable **Deep RAG (beta)**.
3. In chat with that assistant, flip **Deep RAG: On** and ask a multi-hop question
   (e.g. "How does elastic robot orchestration differ between Automation Cloud and
   Automation Suite?").

## Ops notes

- Migration `deeprag0001` is re-parented onto `ckpt_idx_attempt_01` (the SharePoint
  checkpoint migration) to keep a single linear alembic head after the SharePoint
  merge into `feature/darwin`.
- Deep RAG is 2 extra LLM calls + 3–4× retrievals per escalated question; it only
  runs when both flags are on, so simple/default traffic is unaffected.

## Future work

- Rich citations + doc-reference UI for the deep path (map synthesized `[n]` to
  reference docs like the normal `Answer` flow).
- History-aware *first-pass retrieval* — Phase 6 threads history into the prompts,
  but single-shot retrieval still queries the raw message; rephrasing the follow-up
  before the first retrieval would close the residual.
- **Per-sub-question answering + quality-check (map-reduce)** — upstream answers each
  sub-question in isolation (`generate_sub_answer`), drops weak ones
  (`check_sub_answer`), then composes from those sub-answers, discarding the raw
  chunks. We instead keep **all raw chunks** and synthesize holistically in one pass.
  Deliberate trade-off: ours preserves fidelity and cross-sub-question reasoning and
  is cheaper — better for our workload (comparative questions, ≤3 sub-questions,
  moderate chunk counts). Upstream's map-reduce wins on **scale** (compression keeps
  the reduce step within the context window as sub-questions/chunks grow) and
  **noise-robustness** (the per-sub quality gate drops garbage retrievals instead of
  letting them pollute one big prompt). Reach for it only if we raise the
  sub-question cap or target sprawling multi-dimensional questions; the low cap is
  what keeps our one-pass synthesis in the regime where it's the better choice.
- Optional per-doc LLM verification / query expansion (upstream `verify_documents`,
  `expand_queries`) if retrieval noise proves to be the bottleneck.
- Tool/action calling ("B") — structured lookups (e.g. ownership system-of-record)
  for question classes RAG can't answer reliably.

## Upstream comparison (what we deliberately did NOT port)

vs Onyx `deep_search` (main, Mar-2025): we skip the **LangGraph substrate**, the
**always-decompose + second refinement round** (replaced by our verdict gate — their
gate is a stub), **per-sub-question answer generation/check**, **query expansion**,
**per-doc verification**, and **streaming of intermediate reasoning**. We port their
**decomposition** and **compare** prompts. Net: a plain controller that captures the
core multi-hop value at a fraction of the LLM calls, plus a real auto-gate upstream
never built.
