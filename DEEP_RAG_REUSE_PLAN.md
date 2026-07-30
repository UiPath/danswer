# Deep RAG reuse refactor — plan (LOCKED)

Refactor deep-rag so **all answer generation** runs on the well-tested normal answer
flow (`Answer` + `SearchTool`), keeping only the genuinely-novel orchestration custom.
Motivation: deep-rag currently re-implements answer generation twice
(`single_shot`, `multi_hop` synthesis), so it drifts from the normal flow — persona
prompt, citations, quotes, reflexion, doc-pruning, and the authoritative footer all had
to be missing-then-patched. Status: **locked, not implemented.** Deep-rag is beta
(AutomationSuite only), so it's safe to change.

## 1. Issues to address
- Deep-rag duplicates the answer path → parity drift (persona prompt, citations, quotes,
  reflexion, doc-pruning, authoritative footer).
- Refactor so single-shot **and** multi-hop synthesis generate via the normal `Answer`
  infrastructure; deep-rag keeps only: the escalation gate, decomposition, and
  per-sub-question retrieval + collation.

## 2. Important notes (non-obvious, verified)
- **Reuse vehicle:** the normal `Answer` engine + `SearchTool(selected_docs=…)`. Verified:
  the `selected_docs` path runs `prune_documents`, skips the LLM relevance-filter (fine —
  we've curated the chunks), and `Answer` does citations/quotes/reflexion/authoritative-
  footer over whatever docs it's given (`search_tool._build_response_for_specified_sections`).
- **Verdict = a small delimited trailing JSON block** (`---VERDICT--- {coverage, missing}`)
  appended **only on deep-rag's buffered invocation**. NOT global always-emit (would stream
  JSON to users on the normal chat path) and NOT whole-answer JSON (breaks streaming +
  inline `[n]` citation processing + answer quality; fragile validity — the gateway
  `CustomModelServer` has no native `response_format`). The normal streaming flow is
  untouched; `OneShotQAResponse` already returns answer/citations/docs structured, so only
  the verdict needs JSON.
- **Gate = coverage verdict** (complete|partial), NOT the persona confidence score.
- **Fail-open:** missing/unparseable verdict → treat as `complete` (don't escalate); chat
  `force=True` escalates regardless.
- **Kept custom:** `_version_query_variants` (version-normalized retrieval),
  per-sub-question retrieve + collate, `_cap_protected_docs` (per sub-question),
  `DECOMPOSITION_PROMPT`, `COMPARISON_PROMPT` / compare-guard (force-on-complete only).
- **Deleted:** `SINGLE_SHOT_VERDICT_PROMPT` & `SYNTHESIS_PROMPT` answer bodies,
  `_persona_block` / `_persona_prompt_text`, `_apply_authoritative_footer`, and the manual
  `[n]`→doc citation mapping in the chat + Slack deep-rag branches (the `Answer` supplies
  `CitationInfo`/docs directly).
- Deep-rag is buffered on both surfaces (chat deep-branch, Slack `get_search_answer`) → the
  verdict block is collected, never streamed.

## 3. Implementation strategy (files/functions, no code)
- **`deep_rag/controller.py`** — add one shared generator:
  `_generate(question, persona, user, db, *, selected_docs=None, history, want_verdict)
  -> (answer, citations, docs, verdict|None)`:
  build `SearchTool` (`selected_docs=collated` for synthesis, else normal retrieve), build
  `Answer(...)` (persona `PromptConfig`, `answer_style_config` for citations/quotes,
  `force_use_tool`), collect `.processed_streamed_output`; if `want_verdict`, split the
  trailing block → parse coverage/missing, strip from the answer.
  - `run_deep_rag`: single-shot → `_generate(want_verdict=True)`; gate on `coverage`;
    `complete & not force` → return it; else decompose → per-sub-question
    `_persona_retriever` (version-norm) → collate + `_cap_protected_docs` →
    `_generate(selected_docs=collated)`; compare-guard only for force-on-complete.
  - Delete the old `single_shot`/`multi_hop` LLM bodies + the helpers listed above.
- **`prompts/deep_rag.py`** — remove `SINGLE_SHOT_VERDICT_PROMPT`/`SYNTHESIS_PROMPT`; add a
  small verdict-suffix fragment; keep `DECOMPOSITION_PROMPT`, `COMPARISON_PROMPT`.
- **`DeepRagResult`** — carry `citations` + `docs` from the normal flow.
- **`chat/process_message.py`** & **Slack `handle_message.py`** deep-rag branches — consume
  `DeepRagResult.citations`/`docs` directly; delete the manual citation mapping and the
  re-added authoritative footer.

## 4. Tests
- **Unit** (inject a fake `_generate`/`Answer` seam): verdict parse (split + fail-open on
  missing/malformed); gate (complete→no escalation, partial→escalate, force→escalate);
  single-shot uses `selected_docs=None`, multi-hop uses `selected_docs=collated`; keep the
  existing version-normalization + `_cap_protected_docs` + compare-guard tests.
- **Prod-read-only E2E** (isolated-module-load harness): the 25.10-vs-24.10 query still
  returns a both-version comparison AND now carries real citations/docs from the normal
  flow; a normal (non-deep) answer is byte-for-byte unaffected (no verdict suffix).
- Fork has scant coverage + `Answer` is heavy to unit-test → lean on the injected-`_generate`
  seam + the E2E harness.

## Deploy note
Backend-only build; bounce api-server + `dsl`; verify citations render in **both** chat and
Slack deep-rag answers before/after. Deep-rag stays gated to AutomationSuite during rollout.

## Related
`DEEP_RAG.md` (behavior), `deep_rag_live_prod.md` memory (current prod state: vha-238 +
version-diff fix committed on feature/agentic, not yet deployed).
