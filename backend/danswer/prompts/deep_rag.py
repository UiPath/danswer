"""Prompts for the feature-gated Deep RAG flow: question decomposition, the
initial-vs-refined answer compare-guard, and the coverage-gate verdict judge.

Answer generation itself (single-shot and multi-hop synthesis) runs on the normal
`Answer`/`SearchTool` infrastructure — deep RAG no longer re-implements it, and adds
NOTHING to the answer prompt. The coverage gate is `VERDICT_JUDGE_PROMPT`, a separate
post-generation judge call over the finished question+answer; only a `partial` verdict
escalates to the decompose path.
"""

_SEP = "-------"

# `{max_subq}` is a configurable CEILING (the prompt asks for as few as needed).
# `{missing_hint}` optionally seeds the decomposition with the single-shot verdict's
# `missing` list, so the sub-questions target what the first retrieval didn't cover.
DECOMPOSITION_PROMPT = """\
Please create a list of sub-questions whose answers would help to inform the answer to the \
initial question.

First decide how many sub-questions this question ACTUALLY needs — as few as possible, and \
never more than {max_subq}. The right number comes from the question's own structure: one per \
entity/version being compared, one per genuinely distinct sub-topic. A simple single-topic \
question needs only ONE sub-question; do NOT pad the list to the maximum with redundant \
rephrasings of the same ask.

The purpose for these sub-questions could be:
  1) decomposition to isolate individual entities (i.e., 'compare sales of company A and company B' -> \
['what are sales for company A', 'what are sales for company B'])
  2) clarification and/or disambiguation of ambiguous terms
  3) if a term or metric is clear but could relate to various aspects of an entity, create sub-questions \
that are more specific
  4) research individual questions and areas that should really help to ultimately answer the question.

Important:
 - Each sub-question should lend itself to be answered by a RAG system; phrase it that way.
 - Every sub-question MUST be self-contained and retain the full context of the initial \
question: the specific product or subject being asked about, its deployment environment or \
flavor, and any version mentioned. NEVER produce a sub-question about an underlying platform, \
dependency, or technology in isolation from that subject — ask "which operating systems does \
<the product> support on <the platform>?", never "which operating systems does <the platform> \
support?". A sub-question detached from the subject retrieves content about the platform \
itself and contaminates the answer with capabilities the subject does not actually have.
 - If the question COMPARES two or more entities or product versions (e.g. "difference between \
X and Y"), create a SEPARATE sub-question for EACH one on its own — never put both in the same \
sub-question. Each side must be researched independently (its documents are retrieved separately).
 - Don't just decompose; make sure the sub-questions have the proper form (e.g., no 'I').
 - Do NOT create sub-questions that are clarifying questions to the user, make suggestions, or ask the \
user for more information. Take the information from the user as given.

Examples:

Initial Question: Does Automation Suite have support for RHEL 9.x as worker nodes on EKS?
Sub-questions:
What operating systems does Automation Suite support as worker nodes on EKS?
Does Automation Suite support RHEL 9.x as worker nodes on EKS?

(DO NOT produce detached questions stripped of the product name, such as: "What operating systems does EKS support?")

Initial Question: How does Product A compare to Product B regarding encryption on AWS?
Sub-questions:
What encryption capabilities does Product A support on AWS?
What encryption capabilities does Product B support on AWS?

Output ONLY the sub-questions, one per line, with no numbering or extra text.
{subject_block}{missing_hint}{history}Here is the initial question:
{sep}
{question}
{sep}
""".replace(
    "{sep}", _SEP
)

# The compare-guard: keeps the deeper answer only if it is SUBSTANTIALLY better,
# else we fall back to the single-shot. It judges on CORRECTNESS and how directly each
# answer addresses the question — NOT on length/detail/citation count, which the
# (~always longer) refined answer would trivially win, degenerating the guard to
# "deep always wins".
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

# Ours: the coverage-gate verdict, produced by a SEPARATE post-generation judge call
# (fast LLM) over the finished question+answer pair. It used to be a VERDICT_SUFFIX
# appended to the answer prompt itself, but ANY trailing self-assessment task measurably
# flattened the answer's markdown (bullets/bold) into plain prose — so the gate now runs
# outside answer generation entirely, leaving the answer byte-identical to the normal
# flow's. Output contract (the ---VERDICT--- block) is unchanged; the controller parses
# it with the same splitter. Only a `partial` verdict escalates to the decompose path; a
# missing/unparseable verdict fails OPEN (treated as `complete` -> no escalation).
VERDICT_JUDGE_PROMPT = """\
You are the coverage gate of a deep-retrieval pipeline. Below is a user's QUESTION and the \
ANSWER produced from retrieved documentation. Judge whether the answer FULLY and CONFIDENTLY \
addresses every part of the question.

Set "coverage" to "partial" whenever it does not — INCLUDING if the answer hedges, expresses \
low confidence, gives a low confidence score, or defers to / recommends contacting an SME, \
product manager, or another team for any part instead of answering it. Use "complete" ONLY \
when every part of the question is answered with confidence. "missing" lists the specific \
sub-topics the answer did not cover with evidence (an empty list when complete).

QUESTION:
{question}

ANSWER:
{answer}

Respond with exactly this block and nothing else:
---VERDICT---
{{"coverage": "complete" or "partial", "missing": ["<uncovered sub-topic>", ...]}}"""
