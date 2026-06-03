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
1. Vespa returns `NUM_RETURNED_HITS = 50` (+ up to 10 from the prioritized query,
   deduped) — **not 15**.
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

## 5. The source-prioritization bias (and the fix)

`_query_vespa` (`index.py:~705`) historically ran **two** queries and merged them:
```
Query A: all sources,           hits=50
Query B: + source_type ∈ {web, sfkbarticles}, hits=10   (default-on)
merge = B + A; dedup by (doc_id,chunk_id) keeping MAX score; sort desc
```
**Why it's a bug:** the rank profile uses `normalize_linear(...)`, which is
min-max **relative to each query's own candidate set**. Query B's narrow set
normalizes its top docs near the ceiling regardless of absolute relevance; dedup
keeps the inflated B-score. ⇒ `web`/`sfkbarticles` are systematically lifted to
the top by a normalization artifact.

**Interaction with reranking:** rerank only re-scores the **top 15 by this biased
score**, so the bias moves *upstream into candidate selection* — a genuinely
better non-prioritized doc ranked #16 never enters the rerank window. So the hack
**partially undermines** the rerank rollout.

Secondary bugs in the same function: hardcodes `hits` (ignores
`num_to_retrieve`/persona limit) and ignores `offset` (pagination).

**The fix (two paths, this branch):**
- **Reranking ON** ⇒ `prioritize_sources=False` ⇒ a **single all-sources query**
  (one comparable `normalize_linear` scale, honors the caller's `hits`); the
  cross-encoder reorders.
- **Reranking OFF** ⇒ legacy two-query prioritized flow, **byte-for-byte
  unchanged**.

Driven by `prioritize_sources=query.skip_rerank` in `doc_index_retrieval` — so it
rides the same per-assistant + global rerank decision, no separate flag.

> NOTE: the prioritized-source hack is a deliberate fork divergence. The split
> preserves it whenever reranking is off; if you later remove it entirely,
> confirm the original product intent first (curated web/KB content?).

---

## 6. The incremental design (what was built)

**Two-level gate — rerank runs iff `RERANK_ENABLED` (global) AND
`persona.rerank_enabled` (per-assistant).**

- **Global** `RERANK_ENABLED` (env, default false): the master switch. When on, a
  GPU-backed model server warms the reranker and the app *may* rerank. Off (local
  / default) ⇒ reranking never runs ⇒ **no GPU required**.
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

## 7. Infrastructure / GPU plan

Observed on the **darwin** cluster (June 2026):
- **No GPU anywhere** (4 nodes, all `nvidia.com/gpu: <none>`).
- The inference model server (the `INDEXING_ONLY=false` pod) does **query
  embedding + intent** today (~7.3 GiB RAM), with **no resource requests/limits**
  on its container, on a node already at **87% memory** → eviction-prone.
- With rerank off, the cross-encoder is **not even loaded** (`warm_up_cross_encoders`
  is gated). So the reranker is a *net-new* model + per-query compute when enabled.

Decisions:
- **Self-host on a dedicated GPU node**, `Standard_NC6s_v3` (1× V100 16 GB) — more
  than enough (a cross-encoder uses ~1 GB; reranks 15 chunks in <50 ms).
- **Co-locate** embedding + intent + reranker on that GPU node ("deploy rerank +
  other inference models together on GPU") — maximizes the GPU, accelerates query
  embedding, and evacuates the strained CPU node. **Single-GPU-node risk
  accepted** (if it dies, search degrades, not just rerank).
- The existing `danswer-model-server` image **already bundles CUDA torch** — no
  rebuild; it auto-uses the GPU once scheduled there.
- **Reranker model:** `BAAI/bge-reranker-v2-m3` (env-selectable via
  `RERANK_MODEL_NAME`; local keeps the small default). **Switching rerankers needs
  NO reindex** — cross-encoders score chunk *text* at query time; only changing
  the *embedding* model forces a reindex. (Avoid late-interaction/ColBERT-style
  models, which would need indexing changes.)
- **Upstream's stance:** Onyx made the reranker pluggable (default none; local-dev
  = mxbai-xsmall) and **disables local reranking when there's no GPU** (PR #4011)
  — i.e. don't self-host cross-encoder reranking on CPU. Hence the GPU node.

k8s: `k8s/optional/gpu-inference/` component pins the inference deployment to the
GPU pool (`nodeSelector: agentpool=gpupool`, toleration `sku=gpu:NoSchedule`,
`nvidia.com/gpu: 1`, real cpu/mem requests+limits — also fixes the no-limits
smell), and sets `RERANK_MODEL_NAME`. The overlay must also set
`RERANK_ENABLED=true` in `env.properties` (reaches every pod via env-configmap).

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
- `document_index/vespa/index.py` — `_query_vespa(prioritize_sources=...)` single
  vs two-query split; `hybrid_retrieval(prioritize_sources=...)`.
- `document_index/interfaces.py` — `hybrid_retrieval` abstract signature.
- `search/retrieval/search_runner.py` — pass
  `prioritize_sources=query.skip_rerank`.

Web (`web/src/app/admin/assistants/`):
- `interfaces.ts`, `lib.ts`, `AssistantEditor.tsx` — "Rerank results (beta)"
  toggle, mirroring `llm_relevance_filter`.

Infra:
- `k8s/optional/gpu-inference/` — kustomization + inference patch.

Tests (`backend/tests/unit/...`):
- `search/preprocessing/test_resolve_skip_rerank.py` — global × per-assistant
  matrix + explicit override + legacy fallback (7 cases).
- `document_index/vespa/test_query_vespa_prioritization.py` — single-vs-two-query
  split + default-is-legacy (3 cases).

---

## 9. How to enable in prod (when ready)

1. `alembic upgrade head` (adds `persona.rerank_enabled`) → bounce `dapi` + `dbe`.
2. Add the `Standard_NC6s_v3` GPU node pool (label `agentpool=gpupool`, taint
   `sku=gpu:NoSchedule`, NVIDIA device plugin).
3. Prod overlay: add `- ../../optional/gpu-inference` to `components:` **and** set
   `RERANK_ENABLED=true` in `env.properties`. Apply.
4. Toggle **"Rerank results"** on one test assistant → A/B compare against an
   un-toggled copy in chat + Slack → flip the default once satisfied.

Local stays GPU-free with zero config: omit the component, leave `RERANK_ENABLED`
unset → reranking never runs.

---

## 10. Open / sequenced follow-ups

- **Recency tuning is a separate experiment** from reranking — don't bundle.
  Start with `favor_recent` on the test assistant (config); lower the `0.75`
  floor only if needed (schema redeploy). Measure independently.
- **Confirm the intent** of the prioritized-source hack before ever removing it
  outright (it's preserved whenever reranking is off).
- **`enable_auto_detect_filters` is dead** globally (§4) — fixing it would restore
  LLM time/source filter extraction *and* the `auto` recency path; tracked
  separately.
- **No min relevance-score cutoff** (`SEARCH_DISTANCE_CUTOFF=0` unused) — weak
  chunks still fill the context window; candidate for a follow-up.
- **The 5× re-execution on missing citations** (Slack) — latency/cost + orphaned
  chat sessions; candidate for a cap.
- Consider a **stronger/larger reranker** or hosted (Cohere) if `bge-reranker-v2-m3`
  isn't enough — reindex-free either way.
