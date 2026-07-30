"""Prompts for the feature-gated Deep RAG flow.

The decomposition and answer-comparison prompts are ported (and lightly adapted)
from upstream Onyx's Agent Search — `backend/onyx/prompts/agent_search.py`
(INITIAL_QUESTION_DECOMPOSITION_PROMPT, INITIAL_REFINED_ANSWER_COMPARISON_PROMPT).
We reuse upstream's wording deliberately: it is battle-tested across a large user
base. We do NOT use upstream's LangGraph — only the prompt learnings.

The single-shot verdict and synthesis prompts are ours (validated in the
multihop-gate eval): single-shot self-reports coverage, and only a `partial`
verdict escalates to the decompose->synthesize path.
"""

_SEP = "-------"

# Ported from upstream INITIAL_QUESTION_DECOMPOSITION_PROMPT. `{max_subq}` is our
# configurable cap (upstream hard-codes 3). `{missing_hint}` optionally seeds the
# decomposition with the single-shot verdict's `missing` list — upstream's
# "decompose after an initial search" learning, grounded in what we already found.
DECOMPOSITION_PROMPT = """\
Please create a list of no more than {max_subq} sub-questions whose answers would help to \
inform the answer to the initial question.

The purpose for these sub-questions could be:
  1) decomposition to isolate individual entities (i.e., 'compare sales of company A and company B' -> \
['what are sales for company A', 'what are sales for company B'])
  2) clarification and/or disambiguation of ambiguous terms
  3) if a term or metric is clear but could relate to various aspects of an entity, create sub-questions \
that are more specific
  4) research individual questions and areas that should really help to ultimately answer the question.

Important:
 - Each sub-question should lend itself to be answered by a RAG system; phrase it that way.
 - If the question COMPARES two or more entities or product versions (e.g. "difference between \
X and Y"), create a SEPARATE sub-question for EACH one on its own — never put both in the same \
sub-question. Each side must be researched independently (its documents are retrieved separately).
 - Don't just decompose; make sure the sub-questions have the proper form (e.g., no 'I').
 - Do NOT create sub-questions that are clarifying questions to the user, make suggestions, or ask the \
user for more information. Take the information from the user as given.

Output ONLY the sub-questions, one per line, with no numbering or extra text.
{missing_hint}{history}Here is the initial question:
{sep}
{question}
{sep}
""".replace(
    "{sep}", _SEP
)

# Adapted from upstream INITIAL_REFINED_ANSWER_COMPARISON_PROMPT — the compare-guard.
# Keeps the deeper answer only if it is SUBSTANTIALLY better, else we fall back.
# DIVERGENCE FROM UPSTREAM: upstream defines "better" mainly as more comprehensive /
# more detailed / more citations — which the refined (deep) answer, being ~always
# longer, trivially satisfies, so the guard degenerated to "deep always wins". We judge
# on CORRECTNESS and how well each answer actually answers the question, and explicitly
# tell the judge NOT to reward length/detail/citations on their own.
COMPARISON_PROMPT = """\
For the given question, decide which answer is genuinely better — meaning MORE CORRECT and a \
MORE COMPLETE, DIRECT answer to the question — and only say the refined answer wins if it is \
SUBSTANTIALLY better, not just a bit better.

Judge on QUALITY, NOT SIZE. A longer, more detailed, more heavily formatted, or more heavily \
cited answer is NOT automatically better. Do not favor an answer merely for its length, extra \
sections, bullet points, or number of citations.

The refined answer is SUBSTANTIALLY better ONLY if it is clearly more accurate, more directly and \
completely addresses what was actually asked, or fixes a real error or gap in the initial answer. \
If the refined answer is longer but adds vague, off-topic, repetitive, or unsupported/contradictory \
content — or does not answer the question any better than the initial answer — then it is NOT better.

Put yourself in the shoes of the user who asked THIS question: which answer would they consider \
more correct and more useful?

Here is the question:
{sep}
{question}
{sep}

Here is the initial answer:
{sep}
{initial_answer}
{sep}

Here is the refined answer:
{sep}
{refined_answer}
{sep}

Is the refined answer SUBSTANTIALLY better — more correct and a more complete, direct answer to the \
question, not merely longer? Answer with a single word: "yes" or "no".
""".replace(
    "{sep}", _SEP
)

# Ours: single-shot answer + self-reported coverage verdict, in one call. This is
# the gate — only `partial` escalates. Double braces are literal JSON braces.
SINGLE_SHOT_VERDICT_PROMPT = """\
{persona_instructions}Using ONLY the context below, answer the question — applying the assistant \
instructions above to the answer text (its format, SME attribution, confidence handling, and any \
routing/authority rules). Then judge whether the retrieved context was SUFFICIENT to fully answer \
every part of the question.

Respond as strict JSON:
{{"answer": "<your answer, per the assistant instructions above, citing sources as [n]>", \
"coverage": "complete" | "partial", \
"missing": ["<sub-topic the context did NOT cover>", ...]}}

Use "partial" if ANY part of the question is unaddressed or you had to hedge. "missing" lists the specific \
sub-topics still needing evidence (empty list if complete).

Context:
{context}

{history}Question: {question}
JSON:"""

# Ours: synthesize the final answer from the per-sub-question grouped context.
SYNTHESIS_PROMPT = """\
{persona_instructions}Using the retrieved context (grouped by sub-question) below, write a complete \
answer to the ORIGINAL question, following the assistant instructions above (its format, SME \
attribution, confidence handling, and any routing/authority rules). Explicitly compare or combine \
across sub-questions where the question calls for it. Cite sources as [n]. If a required piece is \
genuinely missing from the context, say so.

ORIGINAL question: {question}

{context}

Answer:"""
