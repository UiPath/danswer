# Search Answer Quality: Reranking, Recency, and Retrieval Prioritization

> Design + investigation notes for branch **`feature/improve-queries`**.
> Goal: improve the **reliability of answers** (chat + Slack), give a path to
> **prioritize recent documents**, and do it **incrementally / A-B-comparably**
> rather than flipping a global switch. All findings below were verified against
> the code on this branch; file:line anchors are approximate (they drift as the
> code changes) but point at the right place.

---

## 1. What problem this branch solves

The fork retrieves well but **ranks and reranks poorly by default**, which costs
answer quality:

1. **Cross-encoder reranking is OFF by default** — the LLM receives chunks in raw
   bi-encoder hybrid order, so a genuinely-relevant chunk ranked #12 by vector
   similarity never reaches the prompt (only ~10 chunks fit).
2. **A fork-specific "prioritized source" hack biases retrieval** — it runs a
   second, source-filtered Vespa query and merges it, but because Vespa's
   `normalize_linear` scoring is *relative to each query's candidate set*, the
   narrow second query's scores are inflated and `web`/`sfkbarticles` get lifted
   to the top regardless of true relevance. Default-on for every query. (This was
   removed — but the *recall* goal it served was later restored with a bounded,
   scope-safe pass that doesn't depend on the inflation artifact; see §5.1.)
3. **Recency is only a gentle decay** (and the `auto` setting is effectively
   dead — see §4), so there's no real "prefer recent" behavior.
4. There was **no way to roll any of this out gradually** or compare old vs new.

The branch adds a **two-level rerank gate** (global infra flag + per-assistant
toggle), splits retrieval so reranking gets clean (unbiased) candidates, and
documents the recency levers — without changing behavior for un-opted assistants
or the GPU-free local setup.

---

## 2. The query/answer path (verified)

Both surfaces converge on the same pipeline:

```
Chat:  /chat send-message ─┐
                           ├─► SearchTool.run ─► SearchPipeline
Slack: handle_message ─────┘        │
                                     ├─ retrieval_preprocessing  (builds SearchQuery)
                                     ├─ VespaIndex.hybrid_retrieval → _query_vespa  (retrieve)
                                     ├─ search_postprocessing  (rerank + LLM relevance filter)
                                     └─ prune_documents  (token budget → ~10 chunks → LLM)
```

Key files:
- `danswer/tools/search/search_tool.py` — builds `SearchRequest` (sets `persona`,
  leaves `skip_rerank=None`), runs `SearchPipeline`.
- `danswer/search/pipeline.py` — orchestrates the stages.
- `danswer/search/preprocessing/preprocessing.py` — `retrieval_preprocessing`
  builds the `SearchQuery`; resolves filters, recency multiplier, and
  `skip_rerank` (see §3, §5).
- `danswer/search/retrieval/search_runner.py` — `doc_index_retrieval` calls
  `hybrid_retrieval`.
- `danswer/document_index/vespa/index.py` — `hybrid_retrieval` → `_query_vespa`
  (the Vespa query); `danswer_chunk.sd` is the rank profile.
- `danswer/search/postprocessing/postprocessing.py` — `semantic_reranking`,
  `rerank_chunks`, `filter_chunks`.
- `danswer/danswerbot/slack/handlers/handle_message.py` — Slack entry; now passes
  `skip_rerank=None` so it shares the chat resolver.

### Slack-specific guardrails worth knowing
- No-citations ⇒ **answer suppressed** (primary hallucination guard).
- `@retry(tries=5)` on answer generation; up-to-5× full re-execution on missing
  citations (latency/cost + orphaned chat sessions — a known cost).
- Slack **bypasses ACL** for channels with document sets.

---

## 3. Reranking: what it actually does

**Bi-encoder (default, today):** Vespa scores each chunk as
`alpha·vector_similarity + (1-alpha)·BM25`, then `× document_boost × recency_bias`.
`vector_similarity` is cosine between the query embedding and each chunk's
*pre-computed* embedding — fast, but it can't model fine-grained query↔chunk
interaction.

**Cross-encoder (reranking):** feeds the query **and** each chunk's *text*
together through a transformer (`mxbai-rerank-xsmall-v1` by default) so attention
runs across both → a far more accurate relevance judgment. Standard
retrieve-broad-then-rerank.

**The real flow (corrected mental model):**
1. Vespa returns `NUM_RETURNED_HITS = 50` (a single all-sources query) — **not 15**.
2. `rerank_chunks` reranks only the **top 15** (`NUM_RERANKED_RESULTS`,
   `chunks_to_rerank[:num_rerank]`); the rest get `score=None`, appended behind.
3. `semantic_reranking` computes the cross-encoder score, then
   **`boosted = cross_encoder_score × document_boost × recency_bias`**
   (`postprocessing.py:~74`) and re-sorts. So recency/boost are **re-applied** on
   top of the cross-encoder score — it's not pure cross-encoder.
4. Token-budget prune → ~10 chunks to the LLM (LLM-relevant ones hoisted first).

So **Vespa's score still selects *which* 15 are candidates**; the cross-encoder
reorders within that set. This is why a biased candidate set (see §5) matters.

**Flags (both default `false` → rerank off everywhere):**
- Slack: `skip_rerank = not ENABLE_RERANKING_ASYNC_FLOW`
- Chat: `skip_rerank = not ENABLE_RERANKING_REAL_TIME_FLOW`

This branch supersedes both with `RERANK_ENABLED` + per-assistant (see §6); the
old flags remain as a fallback.

---

## 4. Recency / freshness

The decay machinery **already exists and is identical to upstream Onyx** — there
is nothing to "catch up" on; the lever is *tuning* it.

Vespa rank profile (`danswer_chunk.sd`):
```
document_age = max(if(isNan(doc_updated_at),7890000, now()-doc_updated_at)/31536000, 0)   # years
recency_bias = max(1 / (1 + query(decay_factor) * document_age), 0.75)                     # floored at 0.75
# global-phase: (alpha·norm(vector) + (1-alpha)·norm(keyword)) * document_boost * recency_bias
```
- `decay_factor = DOC_TIME_DECAY(0.5) × recency_bias_multiplier`.
- **Floor 0.75** ⇒ an old doc loses *at most 25%* of its score. It only *decays*
  old docs; it never *boosts* fresh ones.

`recency_bias_multiplier` per persona (`preprocessing.py:~176`): `no_decay`→0,
`base_decay`→0.5, `favor_recent`→1.0, `auto`→LLM-predicted.

**Gotcha — `auto` is effectively dead.** The LLM time-filter auto-detection
(`enable_auto_detect_filters`) is **never threaded through**: `handle_message`
sets it on `RetrievalDetails`, but `SearchTool` doesn't copy it into
`SearchRequest` and `pipeline.py` doesn't pass it to `retrieval_preprocessing`
(whose param defaults `False`). So personas set to `recency_bias: "auto"` (the
seeded default) fall back to *base* decay and never favor recent. → **use
`favor_recent` for a deterministic recency preference.**

**Levers to actually prefer recent (by effort):**
| Lever | Effect | Cost |
|---|---|---|
| persona `recency_bias: favor_recent` | doubles decay rate | config only |
| lower the `0.75` floor in `danswer_chunk.sd` | old docs decay further | Vespa schema redeploy |
| raise `DOC_TIME_DECAY` env | sharper 0–2yr decay | env only (floor-capped) |
| add a real `freshness()` boost term | lifts new docs | schema change + **reindex** |

**Reranking weakens recency further** (a 0.75–1.0 multiplier barely moves a wide
cross-encoder score spread). So if recency matters, tune decay **separately**
from the rerank rollout, and measure independently.

---

## 5. Source prioritization & authoritative citations

**The requirement:** a chatty source (e.g. a busy Slack channel) shouldn't crowd
authoritative **curated** content (`web`/docs, `sfkbarticles`, `highspot`,
`outsystems`) out of the answer — neither out of the prompt nor out of the
**citations** the user sees.

This is a **layered pipeline**, all global + config-gated (no per-assistant knob),
all keyed off **`PROTECTED_SOURCES`** (prod: `web,sfkbarticles,highspot,outsystems`).
Each layer was added because the prior one was necessary-but-insufficient — they
move a doc from *retrieved* → *in the prompt* → *cited*. Everything below applies to
**both** the chat and Slack flows (they share `SearchPipeline` and the `Answer`
object) and **every** assistant.

> Historical note: the fork once ran a **two-query union** in `_query_vespa` (an
> all-sources query plus a source-filtered one). It was removed because the rank
> profile's `normalize_linear(...)` is min-max **relative to each query's candidate
> set**, so the narrow second query over-promoted its docs regardless of true
> relevance. The *recall goal* it served is now met by §5.1 — without that bug.

### 5.1 Recall — `SOURCE_RESERVED_RETRIEVAL_SLOTS` (retrieval)
Final-selection promotion (§5.2) can only reorder docs retrieval already returned.
When a chatty source saturates the top-`NUM_RETURNED_HITS=50`, a relevant curated
doc may not be in the candidate set at all. `SearchPipeline._supplement_protected_sources`
(pure core `protected_source_topup`) runs **one extra source-scoped retrieval** when
fewer than N protected-source chunks are present, and merges the top results in. It
reuses the **same filters** as the main query (ACL + persona document-set fence) and
only **adds** a `source_type` restriction — so it never widens scope; it guarantees
**presence** (ordering is §5.2, so it doesn't rely on the old normalize artifact).
prod `=6`, code default `0` (off). *Note: 6 because a relevant protected doc can rank
#4 among protected sources and miss a smaller cut.*

### 5.2 Prompt position — `SOURCE_DIVERSITY_RESERVED_SLOTS` (final selection)
`ensure_source_diversity` in `doc_pruning.py` (in `_apply_pruning`, after the
relevance reorder, before the token cut) promotes up to N of the highest-ranked
`PROTECTED_SOURCES` docs to the **front** of the prompt, preserving the rest of the
order. prod `=3` (was 2). Disable with `=0`.

### 5.3 Prompt balance — `MAX_PROMPT_DOCS_PER_SOURCE` (final selection)
Even with curated docs at the front, a prompt of `3 curated + 49 Slack` lets the LLM
ground every claim in the dominant source. `cap_docs_per_source` (in `_apply_pruning`,
after `ensure_source_diversity`) keeps the top-N docs **per source** and drops the
rest before the token cut. prod `=8`, code default `0`. Only binds when a source
dominates (single-source assistants unaffected).

### 5.4 Citation preference — authoritative-sources nudge (prompt)
A soft, global instruction (`build_authoritative_sources_reminder` in
`prompt_utils.py`, appended to the shared `CITATION_REMINDER` via
`build_task_prompt_reminders`, derived from `PROTECTED_SOURCES`) asks the model to
prefer citing authoritative sources over chat discussions when they support the
point. Soft — it nudges, it doesn't guarantee.

### 5.5 Citation guarantee — verify-then-retain (`AUTHORITATIVE_CITATION_RETENTION_ENABLED`)
**The hard lesson:** presence/position/balance (5.1–5.3) reliably get curated docs
*into the prompt and into the answer's content*, but **citation attribution is a
separate, harder problem**. With curated docs at prompt positions [1][2][3], the LLM
still cited the near-duplicate Slack threads — and *no* prompt lever (soft nudge,
mandatory "you MUST cite", grouped output) reliably flipped it (the grouped variant
even mislabeled). Citations are the LLM's output; a prompt is a request it can ignore.

So we add a **deterministic post-generation step** in `Answer._process_stream`
(`authoritative_retention.py`): for any **uncited** authoritative doc in context, one
batched LLM call checks whether it's relevant, and relevant ones are appended as an
**"Authoritative sources" footer** (markdown links; renders in chat + Slack). It is:
- **additive** (the LLM's own inline citations are untouched);
- **gated** to *uncited* authoritative docs — citing one KB doesn't suppress surfacing
  another relevant docs/web page;
- **verified on the matched chunk** (`LlmDoc.content`, the retrieved passage, passed
  whole) against **both the question and the answer** — relevance to the *question*
  (not just the answer) is what excludes topically-adjacent docs (e.g. an Azure-SignalR
  or "Automation Cloud cannot be accessed" KB on an "is there AI?" question), while a
  same-subject doc with a scary "error" title (e.g. a "Migration failed … on upgrade"
  KB whose body is about pre-upgrade table cleanup) is correctly kept;
- **conditional + bounded** — at most ONE extra call, only when an uncited
  authoritative doc is present; retries once on a transient gateway timeout; fail-closed.

> Why a footer and not merged into the numbered "Sources" cards: `citation_num` is
> the doc's context position and the LLM already owns the low numbers, so injecting a
> retained doc collides (de-duped away by `translate_citations`, first-wins) and can't
> be placed "at the top" without renumbering the LLM's inline `[[n]]`. The footer
> sidesteps that. The footer/`final_context` links *are* rewritten (§5.6); the LLM's
> inline citation **cards** come from the reference-doc snapshot and are left as-is.

### 5.6 Docs versioning — `rewrite_docs_links` (version-aware)
The docs.uipath.com connector indexes ~6 versions of every page (2022.4 … 2025.10,
plus slug variants), with near-identical content — so which version gets retrieved is
~arbitrary, and the query-time version dedup (`dedupe_doc_versions`) only collapses
versions that were *retrieved*. `rewrite_docs_links` (in `doc_pruning.py`, called from
`search_tool` after prune) resolves each versioned docs link to the right version of
the same page (URL with the version segment stripped, slug kept):
- if the question names exactly one version (`parse_question_doc_version`: "23.10" →
  `2023.10`; multiple = ambiguous → None), resolve to **that** version even if older —
  "is X supported in 23.10?" must point at the 23.10 doc;
- otherwise resolve to the **newest indexed** version.
One PK-indexed prefix-scan per page; no reindex.

---

## 6. The incremental design (what was built)

**Two-level gate — rerank runs iff `RERANK_ENABLED` (global) AND
`persona.rerank_enabled` (per-assistant).**

- **Global** `RERANK_ENABLED` (env, default false): the master switch. When on,
  the reranker is available (served by **TEI on CPU** — see §7 — or a GPU) and
  the app *may* rerank. Off (local / default) ⇒ reranking never runs.
- **Per-assistant** `Persona.rerank_enabled` (bool, default false): which
  assistants actually rerank. Lets you enable it on one assistant, compare
  answers against an un-toggled copy in chat **or** Slack, and flip the default
  once convinced.
- **Single resolver** `_resolve_skip_rerank(explicit, persona)` in
  `preprocessing.py` is the one place both chat and Slack decide reranking
  (`rerank = (RERANK_ENABLED and persona.rerank_enabled) or
  ENABLE_RERANKING_REAL_TIME_FLOW`). Slack now passes `skip_rerank=None` so it
  shares this logic. An explicit `skip_rerank` is honored as-is.

Both chat and Slack respect it because `SearchTool` builds `SearchRequest` with
`skip_rerank=None` + `persona=<selected assistant>`, and preprocessing reads
`search_request.persona`.

---

## 6b. The two assistant knobs + valid combinations

There are **two per-assistant search-quality knobs**, each gated the same way
(global master switch × per-assistant flag, with a per-conversation chat toggle
that ignores the assistant). Source diversity (§5) is **not** a knob — it's
automatic and globally configured.

| Knob | Global flag | Per-assistant | Chat toggle (default) | Needs GPU? |
|---|---|---|---|---|
| **Reranking** (cross-encoder) | `RERANK_ENABLED` | `Persona.rerank_enabled` | off | No — TEI on CPU (§7) |
| **LLM relevance filter** (one-shot, **main LLM**) | `LLM_RELEVANCE_FILTER_ENABLED` | `Persona.llm_relevance_filter` | off | **No** (LLM-only) |

- **LLM relevance filter** is a **single listwise call on the main LLM**
  (`llm_eval_chunks_listwise`, fails open on parse/error), not 15 fast-LLM
  calls. It needs **no GPU**, so it's a cheaper quality tier on its own.
- **Source prioritization & authoritative citations** (§5) is **not** a per-assistant
  knob either — it's the global, always-on layered pipeline (`PROTECTED_SOURCES` +
  `SOURCE_RESERVED_RETRIEVAL_SLOTS`, `SOURCE_DIVERSITY_RESERVED_SLOTS`,
  `MAX_PROMPT_DOCS_PER_SOURCE`, the authoritative nudge,
  `AUTHORITATIVE_CITATION_RETENTION_ENABLED`, and the version-aware docs rewrite). No
  per-assistant or chat decision.
- **Resolution precedence** for the two knobs: chat per-conversation toggle (if
  the request set it) → assistant flag → default. Slack/one-shot always use the
  assistant flag.

**Reranking × relevance filter — all four combinations work** (source diversity
applies underneath all of them):

| `rerank_enabled` | `llm_relevance_filter` | Behavior | Infra |
|---|---|---|---|
| off | off | raw hybrid order (today's default) | none |
| **on** | off | cross-encoder reordering | **TEI on GPU** |
| off | **on** | LLM relevance filter only (1 LLM call) | **none / no GPU** |
| **on** | **on** | rerank **then** relevance-filter | **TEI on GPU** |

The "relevance-filter-only, no GPU" row is the cheap middle tier (just one extra
LLM call); the "rerank-on" rows need the **GPU** TEI reranker (CPU was measured at
24–98 s/query — see §7).

---

## 7. Reranker serving: TEI on GPU (NVIDIA T4)

**Decision: serve the reranker on GPU via the upstream Hugging Face TEI image.**

We first tried **CPU** (the cluster had no GPU). It was functionally correct but
**not interactive-viable**: measured live on prod (`bge-reranker-v2-m3`, 568M,
fp32, 4 vCPU) reranking 15 chunks took **24 s** (passages ≤512 tok) to **98 s**
(longer passages) — ~1.6 s/passage — and the long blocking inference starved the
`/health` endpoint, so the liveness probe killed the pod (503s under load).
`--auto-truncate` caps the tail but not the ~24 s floor; replicas add concurrency,
not single-query speed; fp16 doesn't help on CPU (x86 has no native fp16 matmul —
ORT upcasts to fp32); int8 is only ~2–4×. So reranking moved to **GPU**, where the
same model reranks 15 chunks in **~20–80 ms**.

Why the **upstream image, no custom build**: TEI's **GPU** backend is Candle +
**safetensors**, which `bge-reranker-v2-m3` ships — so the model loads directly.
The custom-image/ONNX-export dance was *only* needed by TEI's **CPU** runtime
(ONNX-Runtime-based; the model has no ONNX weights). On GPU that constraint is
gone, so we point straight at `ghcr.io/huggingface/text-embeddings-inference:turing-1.5`
(`turing` == T4, compute 7.5; use `86-1.5` for A10, `1.5` for A100/H100).

How it's wired:
- A **`tei-rerank`** Deployment (upstream GPU image, `--dtype float16`) on the
  tainted GPU node pool — it tolerates `gpu=true:NoSchedule` and requests
  `nvidia.com/gpu: 1` (only GPU nodes advertise it, which also pins scheduling).
  Model weights download once into a PVC-backed HF cache (`/data`); restarts reuse
  it (no re-download), so no custom image is needed to avoid re-downloads.
- The app's `CrossEncoderEnsembleModel` calls TEI's `/rerank` when
  **`RERANK_SERVER_URL`** is set (scattering TEI's score-sorted reply back to
  passage order); otherwise it uses the legacy model-server path. When TEI is in
  use, our own model server does **not** load the cross-encoder.
- **No reindex** to adopt or switch rerankers — cross-encoders score chunk *text*
  at query time; only changing the *embedding* model forces a reindex. (Avoid
  late-interaction/ColBERT-style models, which would need indexing changes.)

Node pool: **`Standard_NC4as_T4_v3`** (1× T4 16 GB, 4 vCPU, ~$480/mo) tainted
`gpu=true:NoSchedule`. The reranker needs only ~1.5–2 GB VRAM, so the T4 is ample
and GPU compute is never the bottleneck. TEI is stateless → scale with replicas /
an HPA for throughput.

k8s: **`k8s/optional/tei-rerank/`** component (PVC + Deployment + Service, GPU
request + taint toleration, `/health` probes). Included by the **prod** overlay
(local dev loads the cross-encoder in-process — no TEI container). The overlay's
`env.properties` sets `RERANK_ENABLED=true`,
`RERANK_SERVER_URL=http://tei-rerank-service:80`, and
`LLM_RELEVANCE_FILTER_ENABLED=true`. (The earlier CPU `tei-rerank` image and its
ONNX-export Dockerfile were removed in favor of this.)

> If a GPU is ever desired for lowest latency, the same model runs on a **CUDA**
> node (e.g. `NV6ads_A10_v5` — fractional NVIDIA A10; *not* `NV8as_v4`, whose GPU
> is AMD and unusable by TEI/PyTorch). Not needed for current scale.

---

## 8. Implementation map (files changed on this branch)

Backend:
- `db/models.py` — `Persona.rerank_enabled` (server_default false).
- `alembic/versions/f6a7b8c9d0e1_persona_rerank_enabled.py` — migration
  (down_revision `e5f6a7b8c9d0`).
- `db/persona.py` — thread `rerank_enabled` through `upsert_persona` /
  `create_update_persona`.
- `server/features/persona/models.py` — `CreatePersonaRequest` +
  `PersonaSnapshot` + `from_model`.
- `shared_configs/configs.py` — `RERANK_ENABLED`, env-selectable
  `CROSS_ENCODER_MODEL_ENSEMBLE` via `RERANK_MODEL_NAME`.
- `search/preprocessing/preprocessing.py` — `_resolve_skip_rerank` (single
  resolver) + `Persona` import.
- `danswerbot/slack/handlers/handle_message.py` — `skip_rerank=None` (+ dropped
  the now-unused `ENABLE_RERANKING_ASYNC_FLOW` import).
- `model_server/main.py` — warm cross-encoder when `RERANK_ENABLED`.
- `document_index/vespa/index.py` — `_query_vespa` simplified to a **single
  all-sources query** (removed the two-query union).

LLM relevance filter (independent gate, one-shot, main LLM):
- `configs/chat_configs.py` — `LLM_RELEVANCE_FILTER_ENABLED`.
- `preprocessing.py` — `_resolve_skip_llm_chunk_filter` resolver.
- `prompts/llm_chunk_filter.py` + `secondary_llm_flows/chunk_usefulness.py` —
  `LISTWISE_CHUNK_FILTER_PROMPT` + `llm_eval_chunks_listwise` (+ `_parse_useful_indices`).
- `search/pipeline.py` — relevance filter now uses the **main** llm (not fast).
- `search/postprocessing/postprocessing.py` — `filter_chunks` → listwise call.

Source prioritization & authoritative citations (automatic, global — §5):
- `configs/chat_configs.py` — `PROTECTED_SOURCES`, `SOURCE_DIVERSITY_RESERVED_SLOTS`,
  `SOURCE_RESERVED_RETRIEVAL_SLOTS`, `MAX_PROMPT_DOCS_PER_SOURCE`,
  `AUTHORITATIVE_CITATION_RETENTION_ENABLED`.
- `search/pipeline.py` — `_supplement_protected_sources` + pure
  `protected_source_topup` (recall, §5.1).
- `llm/answering/doc_pruning.py` — `ensure_source_diversity` (§5.2),
  `cap_docs_per_source` (§5.3); `rewrite_docs_links` + `parse_question_doc_version`
  + `_versioned_url_parts` (version-aware docs links, §5.6); `dedupe_doc_versions` /
  `_docs_version_sort_key` (version dedup).
- `prompts/prompt_utils.py` — `build_authoritative_sources_reminder` (nudge, §5.4),
  appended in `build_task_prompt_reminders`.
- `llm/answering/authoritative_retention.py` — verify-then-retain footer
  (`select_authoritative_candidates`, `verify_supporting_docs`,
  `retained_authoritative_footer`), hooked in `llm/answering/answer.py`
  `_process_stream` (§5.5).
- `tools/search/search_tool.py` — calls `rewrite_docs_links(...,
  parse_question_doc_version(query))` after `prune_documents`.

Chat per-conversation toggles + TEI serving:
- `server/query_and_chat/models.py` — `use_reranking` / `use_relevance_filter`
  on `CreateChatMessageRequest`.
- `tools/search/search_tool.py` + `chat/process_message.py` — thread the
  per-conversation skips into the `SearchRequest`.
- `shared_configs/configs.py` — `RERANK_SERVER_URL`; `search_nlp_models.py`
  `CrossEncoderEnsembleModel` TEI `/rerank` path; `model_server/main.py` skips
  loading the cross-encoder when TEI serves it.

Web:
- `admin/assistants/{interfaces,lib,AssistantEditor}.tsx` — "Rerank results"
  checkbox (relevance filter reuses the existing "Apply LLM Relevance Filter").
- `chat/{lib.tsx,ChatPage.tsx,input/ChatInputBar.tsx}` — two per-conversation
  chat toggles (Rerank, Relevance).

Infra:
- `k8s/optional/tei-rerank/` — GPU TEI reranker component (upstream TEI image on a
  T4 node pool); included by the **prod** overlay (sets `RERANK_ENABLED` /
  `RERANK_SERVER_URL` / `LLM_RELEVANCE_FILTER_ENABLED`). **Local** loads the
  reranker in-process (no TEI, no GPU), via `RERANK_ENABLED` with
  `RERANK_SERVER_URL` unset.

Tests:
- `tests/unit/.../test_resolve_skip_rerank.py`, `test_resolve_skip_llm_chunk_filter.py`
  — the two gating matrices.
- `tests/unit/.../test_listwise_chunk_filter.py` — listwise parser.
- `tests/unit/.../test_source_diversity.py` — diversity promotion / caps / disable.
- `tests/unit/.../test_source_reserved_topup.py` — recall top-up / dedupe / scope.
- `tests/unit/.../test_cap_docs_per_source.py` — per-source cap.
- `tests/unit/.../test_authoritative_sources.py` — the nudge text from PROTECTED_SOURCES.
- `tests/unit/.../test_authoritative_retention.py` — candidate gate, chunk-based
  verify, fail-closed/retry, footer.
- `tests/unit/.../test_docs_version_rewrite.py` — version parse + version-aware /
  latest rewrite.
- `tests/integration/` — TEI rerank transport (mocked), **real CPU cross-encoder
  reordering** (MiniLM), and `filter_chunks` with a stub LLM.

---

## 9. How to enable in prod (when ready)

1. `alembic upgrade head` (adds `persona.rerank_enabled`) → bounce `dapi` + `dbe`.
2. Add a GPU node pool tainted `gpu=true:NoSchedule` (prod uses
   `Standard_NC4as_T4_v3`). The prod overlay already includes
   `../../optional/tei-rerank` and sets `RERANK_ENABLED` / `RERANK_SERVER_URL` /
   `LLM_RELEVANCE_FILTER_ENABLED` in `env.properties` — `kubectl apply -k
   k8s/overlays/prod`. TEI pulls the upstream GPU image and downloads the model
   once into its PVC-backed cache.
3. Per assistant (admin editor): toggle **Rerank results** and/or **Apply LLM
   Relevance Filter**. Or use the **chat-page toggles** to A/B per conversation.
   Compare answers, then flip the defaults once satisfied. Source prioritization &
   authoritative citations (§5) are automatic and already on in prod —
   `PROTECTED_SOURCES=web,sfkbarticles,highspot,outsystems`,
   `SOURCE_RESERVED_RETRIEVAL_SLOTS=6`, `SOURCE_DIVERSITY_RESERVED_SLOTS=3`,
   `MAX_PROMPT_DOCS_PER_SOURCE=8`, `AUTHORITATIVE_CITATION_RETENTION_ENABLED=true`.

Reranking needs the GPU node; the **relevance filter alone needs no GPU**. Local
exercises reranking in-process (no GPU) for dev.

---

## 10. Open / sequenced follow-ups

- **Recency tuning is a separate experiment** from reranking — don't bundle.
  Start with `favor_recent` on the test assistant (config); lower the `0.75`
  floor only if needed (schema redeploy). Measure independently.
- **Graceful rerank fallback:** if the TEI reranker errors, search currently
  degrades rather than falling back to bi-encoder order — worth adding before
  enabling rerank by default (see §3 / reliability).
- **`enable_auto_detect_filters` is dead** globally (§4) — fixing it would restore
  LLM time/source filter extraction *and* the `auto` recency path; tracked
  separately.
- **No min relevance-score cutoff** (`SEARCH_DISTANCE_CUTOFF=0` unused) — weak
  chunks still fill the context window; candidate for a follow-up.
- **The 5× re-execution on missing citations** (Slack) — latency/cost + orphaned
  chat sessions; candidate for a cap.
- Consider a **stronger/larger reranker** or hosted (Cohere) if `bge-reranker-v2-m3`
  isn't enough — reindex-free either way.
- **Externalize the tuned prompts to a configmap** (verify prompt §5.5, nudge §5.4)
  so prompt iteration doesn't need an image rebuild — file-mounted configmap, read
  with the in-code default as fallback (hot-reload via mounted-file sync). Scoped but
  not yet built; only worth it for the actively-tuned prompts, and watch for
  config-vs-git drift (repo file stays canonical).
- **Authoritative citation: only the footer / `final_context` docs links are
  version-rewritten (§5.6); the LLM's inline citation cards (reference-doc snapshot)
  are not** — deliberate, but means inline cards can show a different version than
  the footer. Revisit only if that inconsistency matters.
- **Indexed-content freshness:** relevance (and the answer) are judged against the
  *indexed* copy of a doc; an edited KB/docs page can diverge from what we indexed,
  so a citation may point to a live doc whose current content differs. Connector
  re-sync cadence, not a verify-logic issue.
- **DB-backed / admin-editable prompts** — the "real" version of prompt
  externalization if tuning becomes frequent or multi-owner; bigger build (table +
  endpoints + UI), not warranted yet.
