# Deep RAG compare-guard: old (length-biased) vs new (correctness) prompt

**Question:** does the compare-guard (`refined_is_better` / `COMPARISON_PROMPT`) actually
decide *which answer is better*, or does it just pick the longer one?

**Method.** Generated real single-shot (`initial`) vs multi-hop (`refined`) answer pairs
via the deep-rag pipeline against **3 prod assistants** (Orchestrator, Integration
Service, Automation Ops), then judged each **same pair** with the OLD prompt and the NEW
prompt. K=4 runs per guard (judge non-determinism). Modeled the *actual* decision in
`refined_is_better`: clean-yes → win, clean-no → lose, **ambiguous → win** (return True).
Run in a prod api-server pod (read-only retrieval, bypass_acl eval-only).

## Result (n=6)

| assistant | question | initial | refined | len ratio | OLD win | NEW win |
|---|---|--:|--:|--:|--:|--:|
| Orchestrator | pending jobs by robot type | 674 | 5084 | 7.5× | 1.00 | 0.00 |
| Orchestrator | robot↔orch connectivity 2022→2023 | 450 | 4080 | 9.1× | 1.00 | 1.00 |
| Integration Service | OAuth vs API-key connections | 478 | 4225 | 8.8× | 1.00 | 0.00 |
| Integration Service | credential/secret asset constraints | 375 | 3001 | 8.0× | 1.00 | 0.00 |
| Automation Ops | governance policy application | 559 | 3512 | 6.3× | 1.00 | 0.00 |
| Automation Ops | pipeline setup | 126 | 3746 | 29.7× | 1.00 | 0.00 |

**Aggregate: refined ~12× longer (6/6 longer) · OLD win-rate = 1.00 · NEW win-rate = 0.17**

## Findings

- **OLD prompt = 100% rubber stamp.** Deep won every time, perfectly tracking "longer"
  (mean length ratio 11.6×). The guard was effectively a no-op. Upstream's
  `INITIAL_REFINED_ANSWER_COMPARISON_PROMPT` has the same unfixed bias.
- **NEW prompt discriminates (17% win).** Keeps the deep answer only where it is genuinely
  more correct/complete. The single retained win (Orchestrator connectivity — machine keys
  vs client credentials across two versions) is a legitimate multi-part case; the other
  five long-but-not-better answers are correctly rejected.
- **Prompt fix is sufficient on its own.** Even with the `ambiguous → win` tie-break left
  unchanged, win dropped to 17%, because the new prompt yields *decisive* "no" verdicts
  instead of ambiguous ones the tie-break was converting to wins. No need to also flip the
  tie-break.

## Caveats

n=6, K=4 — small sample, but the effect is stark (100% vs 17%) and directionally
unambiguous (not a within-noise result). Scale up questions/assistants for tighter
per-question rates if needed.
