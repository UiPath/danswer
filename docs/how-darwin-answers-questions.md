# How Darwin Answers a Question

A visual tour of how Darwin turns a question (in **Slack** or the **web chat**)
into a **grounded, cited answer** — and the knobs that change that behavior.
Written for a mixed audience: enough pictures for a stakeholder conversation,
enough specifics that engineers trust it.

> **TL;DR** — Darwin is **not** a "stuff some chunks into a prompt" toy RAG.
> Every question runs through hybrid retrieval → recency-aware ranking → an
> optional neural reranker → an LLM relevance filter → answer-grounding
> guardrails — all configurable **per assistant**. That depth is where answer
> quality and trust come from.
>
> Engineering deep-dive (file:line, exact ranking math, rollout plan):
> [`search-quality-reranking-and-recency.md`](./search-quality-reranking-and-recency.md).

---

## 1. The moving parts

```mermaid
%%{init: {'theme':'base','themeVariables':{'textColor':'#1e293b','lineColor':'#64748b','edgeLabelBackground':'#ffffff','clusterBorder':'#cbd5e1'}}}%%
flowchart LR
    SL["💬 Slack"]:::surface
    WEB["🖥️ Web chat"]:::surface

    subgraph CORE["🧩 Answering core"]
      direction TB
      API["⚙️ API server<br/>orchestration"]:::core
      PIPE["🔎 Search pipeline"]:::core
      API --> PIPE
    end

    MS["🧠 Inference model server<br/>embeddings · intent · reranker"]:::model
    VES[("📚 Vespa<br/>vector + keyword index")]:::store
    LLM["✨ LLM provider"]:::llm
    PG[("🗄️ Postgres")]:::store
    REDIS[("⚡ Redis<br/>cache · rate-limit")]:::store

    SL --> API
    WEB --> API
    PIPE -->|"embed query"| MS
    PIPE -->|"hybrid search"| VES
    PIPE -->|"generate"| LLM
    API -.-> PG
    API -.-> REDIS

    subgraph ING["📥 Ingestion · runs continuously"]
      direction LR
      CONN["🔌 Connectors<br/>Slack · Jira · Web · Salesforce …"]:::surface
      DASK["🧵 Dask workers<br/>chunk + embed"]:::core
      CONN --> DASK
    end
    DASK -->|"write chunks + vectors"| VES

    classDef surface fill:#dbeafe,stroke:#2563eb,color:#1e3a8a,stroke-width:1px
    classDef core fill:#ede9fe,stroke:#7c3aed,color:#4c1d95,stroke-width:1px
    classDef model fill:#fef3c7,stroke:#d97706,color:#7c2d12,stroke-width:1px
    classDef store fill:#f1f5f9,stroke:#64748b,color:#334155,stroke-width:1px
    classDef llm fill:#fae8ff,stroke:#c026d3,color:#701a75,stroke-width:1px
    style CORE fill:#faf5ff,stroke:#a78bfa,color:#4c1d95
    style ING fill:#f0fdf4,stroke:#86efac,color:#14532d
```

Two independent halves:
- **📥 Ingestion (always on):** connectors pull documents → Dask workers chunk &
  embed them → everything lands in **Vespa**. (Smart enough to *skip*
  re-indexing unchanged content.)
- **🧩 Answering (per question):** the path in §2.

---

## 2. The journey of a question

The same pipeline serves **both** Slack and web chat — they differ only in entry
point and presentation, not in how retrieval/ranking work.

```mermaid
%%{init: {'theme':'base','themeVariables':{'textColor':'#1e293b','lineColor':'#64748b','actorBkg':'#eef2ff','actorTextColor':'#1e293b','actorBorder':'#6366f1','actorLineColor':'#94a3b8','signalColor':'#334155','signalTextColor':'#1e293b','noteBkgColor':'#fef9c3','noteTextColor':'#713f12','noteBorderColor':'#eab308','labelBoxBkgColor':'#e0e7ff','labelBoxBorderColor':'#6366f1','labelTextColor':'#1e293b','sequenceNumberColor':'#ffffff','activationBkgColor':'#e0e7ff','activationBorderColor':'#6366f1'}}}%%
sequenceDiagram
    autonumber
    actor U as 👤 User
    participant API as ⚙️ API
    participant MS as 🧠 Models
    participant V as 📚 Vespa
    participant R as 🎯 Reranker
    participant L as ✨ LLM

    U->>API: question + chosen assistant
    Note over API: preprocess — filters,<br/>recency, rerank decision

    rect rgb(220, 252, 231)
    Note over API,V: ① RETRIEVE
    API->>MS: embed the question
    API->>V: hybrid search<br/>(meaning + keywords, recency-weighted)
    V-->>API: ≈ 50 candidate chunks
    end

    rect rgb(254, 243, 199)
    Note over API,R: ② RERANK · only if enabled for this assistant
    API->>R: re-score top 15<br/>(question + chunk text together)
    R-->>API: reordered by true relevance
    end

    rect rgb(250, 232, 255)
    Note over API,L: ③ GENERATE
    API->>API: relevance filter → keep best ≈ 10
    API->>L: prompt grounded in those chunks
    L-->>API: streamed answer + citations
    end

    rect rgb(254, 226, 226)
    Note over API: 🛡️ guardrail — no citations ⇒ don't answer
    end
    API-->>U: ✅ grounded answer with sources
```

---

## 3. The retrieval funnel — where quality comes from

A question doesn't get "the top chunks." It gets **progressively narrowed** by
increasingly precise (and expensive) stages — broad recall first, sharp
precision last:

```
        hundreds of thousands of indexed chunks
   ████████████████████████████████████████████████   KNOWLEDGE BASE
                          │  ① hybrid retrieval  (meaning + keywords + recency)
              ██████████████████████████              ≈ 50 candidates
                          │  ② cross-encoder rerank   (question + chunk together)
                    ██████████████                    top 15, re-scored
                          │  ③ LLM relevance filter + token budget
                       ████████                       ≈ 10 best chunks
                          │  ④ grounded generation + citation check
                          ▼
                        ✅ 1 trustworthy answer
```

```mermaid
%%{init: {'theme':'base','themeVariables':{'textColor':'#1e293b','lineColor':'#64748b','edgeLabelBackground':'#ffffff','clusterBorder':'#cbd5e1'}}}%%
flowchart TB
    CORP["📚 Entire knowledge base<br/>(100k+ chunks)"]:::broad
    CORP --> S1["① Hybrid retrieval · Vespa<br/>vector + keyword, recency-weighted"]:::retrieve
    S1 --> C50(["≈ 50 candidates"]):::count
    C50 --> S2["② Cross-encoder reranker<br/>reads question + chunk together"]:::rerank
    S2 --> C15(["top 15 re-scored"]):::count
    C15 --> S3["③ LLM relevance filter<br/>+ token budget"]:::filter
    S3 --> C10(["≈ 10 best chunks"]):::count
    C10 --> S4["④ Grounded LLM generation"]:::llm
    S4 --> ANS(["✅ 1 cited answer"]):::answer

    classDef broad fill:#f1f5f9,stroke:#64748b,color:#334155
    classDef retrieve fill:#dcfce7,stroke:#16a34a,color:#14532d
    classDef rerank fill:#fef3c7,stroke:#d97706,color:#7c2d12
    classDef filter fill:#ffedd5,stroke:#ea580c,color:#7c2d12
    classDef llm fill:#fae8ff,stroke:#c026d3,color:#701a75
    classDef count fill:#ffffff,stroke:#94a3b8,color:#0f172a,stroke-dasharray:3 3
    classDef answer fill:#bbf7d0,stroke:#15803d,color:#14532d,stroke-width:2px
```

Why each stage matters:

| Stage | What it does | Why it's not trivial |
|---|---|---|
| **① Hybrid retrieval** | Finds candidates by **meaning** (vector) *and* **exact terms** (keyword/BM25), fused, then weighted by recency | Pure vector misses exact IDs/error codes; pure keyword misses paraphrases. Fusion catches both. |
| **② Reranking** | A neural **cross-encoder** reads the question and each chunk *together* and re-scores | Retrieval embeds the chunk *before* it sees your question; the reranker judges actual relevance and fixes "right doc, ranked too low." |
| **③ Relevance filter** | An LLM pass drops off-topic chunks before answering | Stops near-misses from diluting the prompt → fewer confident-but-wrong answers. |
| **④ Grounded generation** | Answer built only from selected chunks, with citations | If it can't cite, Darwin **stays silent** rather than hallucinate. |

---

## 4. Hybrid search, in one picture

Every candidate's score blends two signals, then is nudged by freshness and human
feedback:

```mermaid
%%{init: {'theme':'base','themeVariables':{'textColor':'#1e293b','lineColor':'#64748b','edgeLabelBackground':'#ffffff','clusterBorder':'#cbd5e1'}}}%%
flowchart LR
    SEM["🧭 Semantic similarity<br/>meaning match (vectors)"]:::sem
    KW["🔤 Keyword match<br/>exact terms (BM25)"]:::kw
    SEM -->|"× α"| MIX(("➕ blend")):::mix
    KW -->|"× (1 − α)"| MIX
    MIX --> BOOST["👍 feedback boost<br/>(promote / bury docs)"]:::boost
    BOOST --> REC["🕒 recency factor<br/>(newer scores higher)"]:::rec
    REC --> SCORE(["⭐ final relevance score"]):::score

    classDef sem fill:#dbeafe,stroke:#2563eb,color:#1e3a8a
    classDef kw fill:#dcfce7,stroke:#16a34a,color:#14532d
    classDef mix fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
    classDef boost fill:#fef3c7,stroke:#d97706,color:#7c2d12
    classDef rec fill:#cffafe,stroke:#0891b2,color:#155e75
    classDef score fill:#bbf7d0,stroke:#15803d,color:#14532d,stroke-width:2px
```

> `score = ( α · semantic + (1 − α) · keyword ) × feedback_boost × recency`
> — **α** is the dial between meaning and exact terms (default leans semantic).

This is the *search engine* under the chatbot — a real ranking system, not a
nearest-neighbor lookup.

---

## 5. Recency — preferring fresh knowledge

Two equally-relevant docs shouldn't tie when one is from last week and one is
three years old. Darwin multiplies each score by a **recency factor** that decays
with document age:

> `recency_factor = max( 1 / (1 + decay × age_in_years),  0.75 )`

```
score multiplier by document age   (1.00 = full score)

           new     6 mo    1 yr    2 yr+
default   ██████  █████▍  █████   ████▌    1.00 → 0.89 → 0.80 → 0.75 (floor)
favor-    ██████  █████   ████▌   ████▌    1.00 → 0.80 → 0.75 → 0.75
recent
```

- A **soft nudge**, not a cliff — a clearly-better old doc can still win.
- The **0.75 floor** caps the penalty at 25% (tunable). Decay strength is set
  **per assistant** (`favor_recent` is more aggressive) or globally.
- The lever for "prefer recent" without discarding authoritative older content.

---

## 6. The knobs — same engine, different behavior

Darwin's behavior is **configured, not hardcoded** — globally and **per
assistant** (each assistant has its own knowledge scope, prompt, and ranking
behavior).

| Knob | Where | Default | When on |
|---|---|---|---|
| **Neural reranking** | global switch **and** per-assistant toggle | raw hybrid order | top candidates reordered by a cross-encoder (sharper relevance) |
| **Recency preference** | per assistant | mild decay | stronger tilt toward recent docs |
| **LLM relevance filter** | per assistant | — | off-topic chunks dropped pre-answer |
| **Citations required** | per Slack channel | — | no citations ⇒ **no answer** (won't bluff) |
| **Source diversity** | automatic (global) | on | guarantees curated KB/web docs aren't crowded out of the prompt by a chatty source |
| **Guardrails** | built in | — | ACL filtering · rate limiting · retry/backoff |

```mermaid
%%{init: {'theme':'base','themeVariables':{'textColor':'#1e293b','lineColor':'#64748b','edgeLabelBackground':'#ffffff','clusterBorder':'#cbd5e1'}}}%%
flowchart LR
    QQ(["❓ Same question"]):::q
    QQ --> A1["🅰️ Assistant A<br/>rerank OFF · broad scope"]:::dim
    QQ --> A2["🅱️ Assistant B<br/>rerank ON · favor-recent · curated"]:::bright
    A1 --> R1(["fast · recall-oriented"]):::dimout
    A2 --> R2(["sharper · fresher · more precise"]):::brightout

    classDef q fill:#e0e7ff,stroke:#4f46e5,color:#312e81,stroke-width:2px
    classDef dim fill:#f1f5f9,stroke:#94a3b8,color:#475569
    classDef bright fill:#fef3c7,stroke:#d97706,color:#7c2d12,stroke-width:2px
    classDef dimout fill:#f8fafc,stroke:#cbd5e1,color:#64748b
    classDef brightout fill:#dcfce7,stroke:#16a34a,color:#14532d,stroke-width:2px
```

This is what makes a **controlled rollout** possible: enable a capability on
*one* assistant, compare answers side-by-side, then make it the default — no
big-bang switch.

---

## 7. Why this isn't a toy RAG

A weekend RAG demo is: embed docs → nearest-neighbor → stuff prompt. Darwin adds
the parts that decide whether answers are **trustworthy at scale**:

```mermaid
%%{init: {'theme':'base','themeVariables':{'textColor':'#1e293b','lineColor':'#64748b','edgeLabelBackground':'#ffffff','clusterBorder':'#cbd5e1'}}}%%
flowchart TB
    subgraph TOY["🧪 Toy RAG"]
      direction TB
      T1["vector nearest-neighbor"]:::t --> T2["stuff into prompt"]:::t --> T3["hope it's right"]:::t
    end
    subgraph DARWIN["🦾 Darwin"]
      direction TB
      D1["hybrid retrieval · meaning + keywords"]:::d
      D2["two-stage ranking · recall → precision"]:::d
      D3["recency-aware scoring"]:::d
      D4["LLM relevance filtering"]:::d
      D5["grounded + cited · suppress if unsure"]:::d
      D6["per-assistant config · ACL · rate-limit · retention"]:::d
      D1 --> D2 --> D3 --> D4 --> D5 --> D6
    end
    classDef t fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
    classDef d fill:#dcfce7,stroke:#16a34a,color:#14532d
    style TOY fill:#fef2f2,stroke:#fca5a5,color:#7f1d1d
    style DARWIN fill:#f0fdf4,stroke:#86efac,color:#14532d
```

- **Two-signal hybrid retrieval** (meaning *and* keywords), not just vectors.
- **Two-stage ranking** — cheap recall (bi-encoder) then expensive precision
  (cross-encoder) — the standard of serious search systems.
- **Recency-aware ranking** so stale content doesn't masquerade as current.
- **LLM relevance filtering** before generation.
- **Grounded, cited answers with hallucination suppression** — would rather say
  nothing than make something up.
- **Per-assistant configurability** — different teams, knowledge scopes, prompts,
  and ranking behavior from one platform.
- **Enterprise plumbing** — permissions/ACL, rate limiting, retention, a broad
  connector ecosystem, and a horizontally-scaled indexing pipeline.
- **Operational depth** — separate embedding/indexing/reranking model servers,
  Redis caching, CPU-optimized reranking (TEI), incremental & measurable rollouts.

Each layer is a deliberate quality or trust decision. That's the "meat": the gap
between a chatbot that *sounds* right and one you can put in front of the
business.

---

*Companion deep-dive:*
[`search-quality-reranking-and-recency.md`](./search-quality-reranking-and-recency.md)
*(architecture, exact ranking math, file references, rollout plan).*
