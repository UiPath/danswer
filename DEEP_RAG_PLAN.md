# Deep RAG — locked implementation plan (feature/agentic)

Feature-gated agentic multi-hop RAG. Off changes nothing; on enables verdict-gated escalation.

## Behavior (locked)
- **Assistant-level**: `Persona.deep_rag_enabled` (default **off**) — pilot per assistant.
- **Chat-level override**: `deep_rag` flag on the message request, default **off**.
- **deep-rag OFF** → pure single-shot (today's behavior; no added cost/latency).
- **deep-rag ON** → single-shot **+ self-verdict gate** → escalate to decompose→synthesize only
  when the verdict is `partial` → **compare-guard** (keep single-shot if deep didn't beat it).
  "On" = escalation *allowed*; a simple question single-shot nails still won't decompose.

## Grounded in the eval (see multihop-*.md)
- Verdict gate = the only signal with 100% recall (never silently ships the worse answer).
- Retrieval score is NOT a usable gate signal (no separation).
- Compare-guard makes the ~28% false-positive escalations harmless.

## Upstream learnings (main = onyx, Mar-2025), NOT the graph
- Port prompts: `INITIAL_QUESTION_DECOMPOSITION_PROMPT` (≤3 sub-Qs, entity-isolation, RAG-phrased),
  `INITIAL_REFINED_ANSWER_COMPARISON_PROMPT` (the compare-guard). Adapt onyx→danswer.
- Upstream's auto-gate is a stub (`decide_refinement_need=True`); we are ahead with the real verdict.
- We keep the manual toggle as the override (how upstream ships to its user base).
- Reranker GPU-gated upstream too; our disabled reranker degrades gracefully, not a blocker.

## Phases
1. **Backend core** — `danswer/deep_rag/` controller + `danswer/prompts/deep_rag.py`. Pure, unit-tested.
   Retrieval via `SearchPipeline` with the **real user ACL** (never `bypass_acl`).
2. **Persona flag** — `deep_rag_enabled` column + alembic migration + admin assistant-page toggle.
3. **Chat wiring** — `deep_rag` override on the message request; branch iff `persona.deep_rag_enabled and request.deep_rag`.
4. **Chat UI** — composer toggle, shown only when the active assistant is enabled; default off.
5. **Docs** — AGENTS.md section + DEEP_RAG.md design doc (gate, eval results, upstream learnings).

## Process/migration (per CLAUDE.md)
- New Persona column → `alembic upgrade head` on **local first** (local DB lags prod), then bounce
  api-server + background jobs. Chat schema change → api-server bounce. Web toggle: verify the flag
  reaches the wire ("field reaches the wire" footgun).

## Tests
Controller units: complete→no escalation; partial→escalates; decomposition cap respected;
compare-guard keeps single-shot when deep isn't better; fail-open to single-shot on any deep error.
