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
   to the top regardless of true relevance. Default-on for every query.
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

## 5. Source diversity: keeping KB/web from getting lost

**The requirement:** if a chatty source (e.g. Slack) produces the
highest-relevance chunks, it shouldn't crowd authoritative **KB/web doc**
content out of the ~10 chunks that reach the LLM.

**What the fork used to do (removed on this branch):** `_query_vespa` ran **two**
queries and merged them — an all-sources query plus a second, source-filtered
query — to force `web`/`sfkbarticles` in. That was the wrong mechanism: the rank
profile uses `normalize_linear(...)`, which is min-max **relative to each query's
own candidate set**, so the narrow second query's top docs normalized near the
ceiling **regardless of absolute relevance**. Result: prioritized sources were
*over-promoted* by a normalization artifact, and because rerank/relevance only
see the **top-N candidate window**, the inflated ordering polluted what those
stages got to evaluate.

**What we do instead:** `_query_vespa` now issues a **single, comparably-scored
query**, and source diversity is enforced at **final doc selection** —
`ensure_source_diversity` in `llm/answering/doc_pruning.py` (called from
`_apply_pruning`, after the relevance reorder, before the token-budget cut):

- It promotes up to **`SOURCE_DIVERSITY_RESERVED_SLOTS`** (default **2**) of the
  highest-ranked **`PROTECTED_SOURCES`** (default `web,sfkbarticles`) docs to the
  front, preserving the rest of the order. Actual promotion is
  `min(reserved, #protected docs present)`.
- It's **always-on and globally configured** (env), operates on the **single
  comparably-scored** candidate set (no inflation, plays correctly with rerank),
  and is **not a per-assistant decision** — so it doesn't add an assistant knob.
- Disable with `SOURCE_DIVERSITY_RESERVED_SLOTS=0`.

So the *goal* of the old prioritized-source hack is preserved (KB/web aren't
lost), without the score-inflation bug and without a per-assistant toggle.

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
- **Source diversity** (KB/web protected from being crowded out) is handled
  automatically at doc selection — global env (`PROTECTED_SOURCES`,
  `SOURCE_DIVERSITY_RESERVED_SLOTS`), no per-assistant or chat decision (§5).
- **Resolution precedence** for the two knobs: chat per-conversation toggle (if
  the request set it) → assistant flag → default. Slack/one-shot always use the
  assistant flag.

**Reranking × relevance filter — all four combinations work** (source diversity
applies underneath all of them):

| `rerank_enabled` | `llm_relevance_filter` | Behavior | Infra |
|---|---|---|---|
| off | off | raw hybrid order (today's default) | none |
| **on** | off | cross-encoder reordering | **TEI-CPU** (or GPU) |
| off | **on** | LLM relevance filter only (1 LLM call) | **none / no GPU** |
| **on** | **on** | rerank **then** relevance-filter | **TEI-CPU** (or GPU) |

The "relevance-filter-only, no GPU" row is the cheap middle tier; the "rerank-on"
rows need the TEI reranker but still **no GPU**.

---

## 7. Reranker serving: TEI on CPU (no GPU)

**Decision: serve the reranker on CPU via Hugging Face TEI — no GPU.**

The darwin cluster has **no GPU** (4 nodes, all `nvidia.com/gpu: <none>`). The
*naive* path (our model server's `sentence_transformers.CrossEncoder` on CPU) is
seconds-slow — which is why upstream Onyx disables local rerank without a GPU
(PR #4011). But that's an artifact of the **unoptimized runtime**, not the model:
with an optimized CPU runtime (**TEI** — Rust, native token batching), the same
`BAAI/bge-reranker-v2-m3` (568M) reranks ~20 chunks in **~100–250 ms on CPU at
full precision** — acceptable for the retrieval phase, and **no accuracy loss**
vs a GPU (CPU vs GPU doesn't change the math; only INT8 *quantization* would, and
we don't use it).

How it's wired:
- A **`tei-rerank`** deployment runs **our own image** (`tei-reranker:bge-v2-m3-*`,
  built from `k8s/optional/tei-rerank/Dockerfile`) — TEI's CPU base with
  `bge-reranker-v2-m3` **exported to ONNX and baked in** at `/model`. The CPU
  TEI runtime is ONNX-Runtime-based and the model ships no ONNX weights, so we
  export them at build time (HF Optimum) — this also means **no runtime
  download, no re-download on restart, no HuggingFace runtime dependency**.
- The app's `CrossEncoderEnsembleModel` calls TEI's `/rerank` when
  **`RERANK_SERVER_URL`** is set (scattering TEI's score-sorted reply back to
  passage order); otherwise it uses the legacy model-server path. When TEI is in
  use, our own model server does **not** load the cross-encoder.
- **No reindex** to adopt or switch rerankers — cross-encoders score chunk *text*
  at query time; only changing the *embedding* model forces a reindex. (Avoid
  late-interaction/ColBERT-style models, which would need indexing changes.)

Sizing / scaling (per replica): **~4 vCPU, request 4 GiB / limit 8 GiB** (weights
~2.3 GB FP32 + batch headroom). TEI is stateless → scale with replicas or an HPA
on CPU; rule of thumb ~1 replica per ~5 sustained rerank-QPS. Set CPU
request==limit for predictable latency. Optional INT8 later trades a small
accuracy hit for ~2× speed / ~0.6 GB.

k8s: **`k8s/optional/tei-rerank/`** component (Deployment + Service, CPU
resources, `/health` probes, model-cache volume). Included by **both** the prod
and local overlays. The overlay's `env.properties` sets `RERANK_ENABLED=true`,
`RERANK_SERVER_URL=http://tei-rerank-service:80`, and
`LLM_RELEVANCE_FILTER_ENABLED=true`. (The earlier GPU `gpu-inference` component
was removed in favor of this.)

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

Source diversity (automatic, global — replaces the old two-query prioritization):
- `configs/chat_configs.py` — `PROTECTED_SOURCES`, `SOURCE_DIVERSITY_RESERVED_SLOTS`.
- `llm/answering/doc_pruning.py` — `ensure_source_diversity`, called in
  `_apply_pruning` after the relevance reorder.

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
- `k8s/optional/tei-rerank/` — CPU TEI reranker component; included by the **prod**
  overlay (sets `RERANK_ENABLED` / `RERANK_SERVER_URL` /
  `LLM_RELEVANCE_FILTER_ENABLED`). **Local** loads the reranker in-process (no
  TEI), via `RERANK_ENABLED` with `RERANK_SERVER_URL` unset. (Replaced the
  removed `gpu-inference`.)

Tests:
- `tests/unit/.../test_resolve_skip_rerank.py`, `test_resolve_skip_llm_chunk_filter.py`
  — the two gating matrices.
- `tests/unit/.../test_listwise_chunk_filter.py` — listwise parser.
- `tests/unit/.../test_source_diversity.py` — diversity promotion / caps / disable.
- `tests/integration/` — TEI rerank transport (mocked), **real CPU cross-encoder
  reordering** (MiniLM), and `filter_chunks` with a stub LLM.

---

## 9. How to enable in prod (when ready)

1. `alembic upgrade head` (adds `persona.rerank_enabled`) → bounce `dapi` + `dbe`.
2. The prod overlay already includes `../../optional/tei-rerank` and sets
   `RERANK_ENABLED` / `RERANK_SERVER_URL` / `LLM_RELEVANCE_FILTER_ENABLED` in
   `env.properties` — `kubectl apply -k k8s/overlays/prod`. The reranker image
   has the ONNX model baked in (build/push it from
   `k8s/optional/tei-rerank/Dockerfile`), so it starts fast with **no runtime
   download and no GPU**.
3. Per assistant (admin editor): toggle **Rerank results** and/or **Apply LLM
   Relevance Filter**. Or use the **chat-page toggles** to A/B per conversation.
   Compare answers, then flip the defaults once satisfied. Source diversity is
   automatic (tune via `PROTECTED_SOURCES` / `SOURCE_DIVERSITY_RESERVED_SLOTS`).

No GPU required at any point. Local mirrors prod (the `local` overlay includes
the same TEI component), so reranking can be exercised locally.

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
