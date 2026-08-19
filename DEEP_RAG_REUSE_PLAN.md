# Deep RAG reuse refactor — plan (IMPLEMENTED, not yet deployed)

> **Status 2026-07-30:** implemented on `feature/sharepoint_checkpoint_resume` (working
> tree, uncommitted). All 6 stages done; gates (black/ruff/mypy) clean, 19 controller unit
> tests pass, full import graph loads (no circular import). NOT committed / deployed —
> prod E2E + user sign-off pending. Two behavioral consequences of the reuse, worth noting:
> 1. **Single-shot now uses the normal retrieval + query rephrase** (persona `num_chunks`,
>    relevance-filter, rephrase) instead of the old raw 15-chunk retrieve. Version-variant
>    expansion (`_version_query_variants`) now applies to **multi-hop sub-questions only**;
>    a version-comparison single-shot typically reports `partial` and escalates anyway.
> 2. **Deep-rag answers now go through the one-shot persistence path** (each `generate`
>    call creates a throwaway `one_shot=True` chat session + search-doc rows, exactly like
>    every normal Slack/one-shot answer already does). So a prod validation run WRITES
>    benign one-shot rows — it is no longer the pure read-only module-load of the old
>    controller. Validate accordingly.



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

## Slack formatting (mistune) — already covered, with one requirement
The mistune Slack renderer lives in `build_qa_response_blocks` → `render_answer_markdown`
(blocks.py), which runs on EVERY Slack answer's text — downstream of answer generation. So
the refactored deep-rag answer (from the normal flow) renders through mistune automatically;
no extra wiring. **Requirement:** the trailing `---VERDICT---` block MUST be stripped in the
deep-rag caller BEFORE the answer reaches `build_qa_response_blocks`, or the verdict JSON
would be rendered (and mistune-formatted) into the Slack message. (Same applies to the web
chat render path.)

## Deploy note
Backend-only build; bounce api-server + `dsl`; verify citations render in **both** chat and
Slack deep-rag answers before/after. Deep-rag stays gated to AutomationSuite during rollout.

## Related
`DEEP_RAG.md` (behavior), `deep_rag_live_prod.md` memory (current prod state: vha-238 +
version-diff fix committed on feature/agentic, not yet deployed).
