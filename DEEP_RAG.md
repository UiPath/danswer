# Deep RAG (feature-gated, verdict-gated multi-hop)

Deep RAG lets an assistant escalate a hard question to a multi-step retrieval loop
(decompose → retrieve per sub-question → synthesize) when a single-shot answer is
judged incomplete. It is **off by default** and gated on two flags; when off, the
chat path is exactly as before.

## Behavior

| assistant `deep_rag_enabled` | chat request `deep_rag` | behavior |
|---|---|---|
| assistant `deep_rag_enabled` | chat toggle (per question) | behavior |
|---|---|---|
| off (not rolled out) | shown; **defaults OFF**, user can flip on | on → Deep RAG forced; off → single-shot |
| on (rolled out) | shown; **defaults ON**, user can flip off | on → Deep RAG forced; off → single-shot |

Two independent controls:
- **Assistant lever** (`deep_rag_enabled`) = the gradual-rollout knob. It sets the chat
  toggle's *default* state for that assistant. It no longer gates whether Deep RAG can
  run.
- **Chat toggle** (`request.deep_rag`) = a **per-question override, always available on
  every assistant**. The backend runs Deep RAG purely on this flag, regardless of the
  assistant lever — so a user can force it even on a not-yet-rolled-out assistant, or
  turn it off on a rolled-out one.

When the toggle is on it **forces** the deep path (`force=True`) regardless of the
single-shot coverage verdict; the compare-guard still keeps single-shot if the deep
answer isn't substantially better, so forcing can't produce a *worse* answer — only
extra work. The verdict-gated *adaptive* mode (`force=False`, escalate only on
`partial`) is retained in the controller for a future "deep-by-default" behavior.

**Surfaces:**
- **Web chat** — per-question toggle; when on it **forces** the deep path (`force=True`).
- **Slack bot** — no per-message toggle, so it's **assistant-gated + adaptive**: if the
  selected assistant has `deep_rag_enabled`, run `force=False` (single-shot → escalate
  only on a `partial` verdict → compare-guard). Retrieves with `bypass_acl` like the
  bot's normal answer flow; surfaces "🔎 Deep-searched N angles" when it escalated; fails
  open to the normal Slack answer. See `danswerbot/slack/handlers/handle_message.py`.
- **Web search tab** — intentionally **excluded** (auto-routes the assistant, so a
  per-question override doesn't fit). Answer Trace still records its routing decision.

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
- **Documents + citations ARE surfaced.** The deep-rag chat branch persists the
  retrieved sections as search docs, yields a `QADocsResponse` (so the sources panel
  shows and the UI does NOT render "the AI decided this query didn't need a search"),
  and maps each `[n]` marker → its cited section's `document_id` → saved doc, so
  citations resolve. `cited_source_count` on the badge is the count of **distinct
  cited documents** (not a citation-marker proxy).
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

## Synthesis approach: holistic one-pass vs map-reduce (MEASURED)

We use **holistic one-pass synthesis** — retrieve per sub-question, then feed *all*
raw chunks (grouped by sub-question) into a single synthesis call. Upstream instead
uses **map-reduce**: answer each sub-question in isolation, drop the raw chunks, and
compose from the intermediate sub-answers. We tested which is better rather than
argue it (harnesses + full results: `synthesis-ab-eval.md`,
`synthesis-multirun-eval.md`; decomposition + retrieval held constant, blind judge
temp 0, order counterbalanced).

**Result — holistic wins, decisively and stably:**
- **Completeness**: holistic **4.6** vs map-reduce **2.8** (1–5), tiny variance
  (±0.4) across every run. Composing from compressed sub-answers loses real detail.
- **Head-to-head**: holistic **10 / 5** over map-reduce.
- **Cross-attribution** (stating entity X has a property that belongs only to Y):
  holistic **47%** vs map-reduce **33%** over 15 runs each — **within noise**
  (overlapping CIs), and map-reduce is *not* clean. Both mis-attribute ~30–50% on
  hard composite questions; it's a property of the question/corpus/model, **not** the
  synthesis architecture.

**Methodology lesson (important):** a single-run A/B showed holistic 57% vs
map-reduce 14% cross-attribution and nearly triggered a "fix." Multi-run collapsed
that to 47% vs 33% (indistinguishable) — the gap was run-to-run non-determinism. The
same question flipped its cross-attribution flag across identical runs. **Do not act
on single-run judge flags for this pipeline; estimate rates over ≥5 runs.**

**Decisions:** keep holistic; do **not** adopt map-reduce or an attribution
"guardrail" (a guardrail prompt fixed only 1/3 and is moot given no real difference).
If cross-attribution ever needs reducing, the lever is *upstream of synthesis*
(retrieval quality / reranker / per-claim verification), not the synthesis structure.
The low sub-question cap (3) keeps chunks within one context window, which is what
makes one-pass synthesis viable.

## Future work

- History-aware *first-pass retrieval* — Phase 6 threads history into the prompts,
  but single-shot retrieval still queries the raw message; rephrasing the follow-up
  before the first retrieval would close the residual.
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
