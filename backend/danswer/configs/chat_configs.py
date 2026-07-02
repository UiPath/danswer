import os


PROMPTS_YAML = "./danswer/chat/prompts.yaml"
PERSONAS_YAML = "./danswer/chat/personas.yaml"

NUM_RETURNED_HITS = 50
NUM_RERANKED_RESULTS = 15

# May be less depending on model
MAX_CHUNKS_FED_TO_CHAT = float(os.environ.get("MAX_CHUNKS_FED_TO_CHAT") or 10.0)
# For Chat, need to keep enough space for history and other prompt pieces
# ~3k input, half for docs, half for chat history + prompts
CHAT_TARGET_CHUNK_PERCENTAGE = 512 * 3 / 3072

# For selecting a different LLM question-answering prompt format
# Valid values: default, cot, weak
QA_PROMPT_OVERRIDE = os.environ.get("QA_PROMPT_OVERRIDE") or None
# 1 / (1 + DOC_TIME_DECAY * doc-age-in-years), set to 0 to have no decay
# Capped in Vespa at 0.5
DOC_TIME_DECAY = float(
    os.environ.get("DOC_TIME_DECAY") or 0.5  # Hits limit at 2 years by default
)
BASE_RECENCY_DECAY = 0.5
FAVOR_RECENT_DECAY_MULTIPLIER = 2.0
# Currently this next one is not configurable via env
DISABLE_LLM_QUERY_ANSWERABILITY = QA_PROMPT_OVERRIDE == "weak"
DISABLE_LLM_FILTER_EXTRACTION = (
    os.environ.get("DISABLE_LLM_FILTER_EXTRACTION", "").lower() == "true"
)
# Whether the LLM should evaluate all of the document chunks passed in for usefulness
# in relation to the user query
DISABLE_LLM_CHUNK_FILTER = (
    os.environ.get("DISABLE_LLM_CHUNK_FILTER", "").lower() == "true"
)
# Global master switch for the (one-shot, main-LLM) relevance filter, mirroring
# RERANK_ENABLED. When true the app may run the filter for assistants/chats that
# opt in; when false (default) it never runs regardless of per-assistant flags.
# Unlike reranking this is LLM-only — it needs NO GPU — so it can be enabled on
# its own as a cheaper quality tier. DISABLE_LLM_CHUNK_FILTER still hard-kills it.
LLM_RELEVANCE_FILTER_ENABLED = (
    os.environ.get("LLM_RELEVANCE_FILTER_ENABLED", "").lower() == "true"
)
# Source diversity at final doc selection: guarantee that up to
# SOURCE_DIVERSITY_RESERVED_SLOTS of the highest-ranked docs from PROTECTED_SOURCES
# survive into the LLM prompt, so curated KB/web content isn't crowded out by a
# chatty high-relevance source (e.g. Slack). Replaces the old two-query
# source-prioritization hack — always-on, global, operates on the single
# comparably-scored candidate set. Set RESERVED_SLOTS=0 to disable.
PROTECTED_SOURCES = [
    s.strip().lower()
    for s in (os.environ.get("PROTECTED_SOURCES") or "web,sfkbarticles").split(",")
    if s.strip()
]
SOURCE_DIVERSITY_RESERVED_SLOTS = int(
    os.environ.get("SOURCE_DIVERSITY_RESERVED_SLOTS") or 2
)
# Source-reserved RETRIEVAL (recall guarantee). SOURCE_DIVERSITY_RESERVED_SLOTS
# above only reserves FINAL-prompt slots among docs retrieval already surfaced —
# it cannot help when a chatty source (e.g. Slack) saturates the entire top-N and
# a curated PROTECTED_SOURCES doc (web/KB/OutSystems) never makes the candidate set
# at all. This runs ONE extra source-scoped retrieval pass (reusing the same ACL +
# persona doc-set fence) to guarantee up to N PROTECTED_SOURCES docs land in the
# candidate set, then the diversity reservation above carries them into the prompt.
# 0 = disabled (single-query behavior). Set per environment.
SOURCE_RESERVED_RETRIEVAL_SLOTS = int(
    os.environ.get("SOURCE_RESERVED_RETRIEVAL_SLOTS") or 0
)
# Per-source cap on the FINAL LLM prompt. A chatty source (e.g. a busy Slack
# channel) can contribute dozens of docs and monopolize what the LLM grounds in
# and CITES, drowning out curated sources even when those are present and
# front-ranked. This keeps the top-N (highest-ranked, after source-diversity
# promotion) docs per source and drops the rest before the token-budget cut, so
# the model sees a balanced set and cites across sources. 0 = disabled (no cap).
# Generic: applies to every assistant + both flows, and only binds when one
# source dominates (single-source assistants are unaffected).
MAX_PROMPT_DOCS_PER_SOURCE = int(os.environ.get("MAX_PROMPT_DOCS_PER_SOURCE") or 0)
# Verify-then-retain authoritative citations. Citations are the LLM's output and it
# inconsistently cites curated sources even when they're at the front of the prompt.
# After generation, if a promoted PROTECTED_SOURCES doc is in context but was NOT
# cited by the LLM, one extra (conditional, batched) LLM call checks whether it
# actually supports a statement in the answer; supporting docs are appended as an
# "Authoritative sources" footer. Additive (LLM's own citations are untouched),
# deduped (skips already-cited + same-document_id), honest (only retains on verified
# support). 0/false = disabled. Costs at most ONE extra call, and only on answers
# where an uncited authoritative doc is present.
AUTHORITATIVE_CITATION_RETENTION_ENABLED = (
    os.environ.get("AUTHORITATIVE_CITATION_RETENTION_ENABLED", "").lower() == "true"
)
# Optional model override for the assistant ROUTER (the one-shot Search tab's
# automatic assistant picker). When BOTH are set, routing uses this gateway
# vendor/model instead of the default fast model — e.g. point it at Claude
# (ASSISTANT_ROUTER_LLM_VENDOR=awsbedrock + the gateway's Claude model id) for
# sharper assistant selection. Empty => use the default fast LLM. Note: a heavier
# model improves selection precision but adds latency/cost to the (already extra)
# router call; routing is a classification task that the fast model handles well,
# so treat this as an A/B lever rather than a default.
ASSISTANT_ROUTER_LLM_VENDOR = os.environ.get("ASSISTANT_ROUTER_LLM_VENDOR") or ""
ASSISTANT_ROUTER_LLM_MODEL = os.environ.get("ASSISTANT_ROUTER_LLM_MODEL") or ""
# How many assistants the auto-routed Search tab's router ranks per question. The
# #1 answers the question (single scope = its own document sets); ranks 2..N are
# surfaced as "recommended assistants" the user can chat with next if #1 wasn't
# right. Default 3 => 1 answerer + up to 2 recommendations.
AUTO_SEARCH_TOP_N = int(os.environ.get("AUTO_SEARCH_TOP_N") or 3)
# Semantic intent pre-route (between the keyword pre-route and the LLM instruction
# router). An LLM matches the question against all assistants' routing_intents
# phrases in one call; it routes only when the LLM's reported confidence is >= this
# value. High by default, because a fire is a deterministic route that skips the
# instruction router — raise it to fire less (more falls through to the router).
AUTO_SEARCH_INTENT_THRESHOLD = float(
    os.environ.get("AUTO_SEARCH_INTENT_THRESHOLD") or 0.8
)
# Versioned-docs dedup at final doc selection. Documentation sites publish the
# SAME page under one URL per product version (e.g. docs.uipath.com/.../2024.10/…
# and /.../2023.10/… and /.../2.2510/…). Retrieval then floods the LLM context
# with many near-identical copies of one page, crowding out distinct sources and
# (observed) making the LLM intermittently fail to cite -> DanswerBot skips the
# answer. When enabled, for each docs page we keep only the newest version's
# chunk(s) and drop the older-version duplicates, freeing context for diverse
# pages. Scoped to URLs containing DOCS_VERSION_DEDUP_URL_SUBSTR — all other
# sources are untouched. Set the substr empty to disable.
DOCS_VERSION_DEDUP_URL_SUBSTR = (
    os.environ.get("DOCS_VERSION_DEDUP_URL_SUBSTR") or "docs.uipath.com"
)
# Whether the LLM should be used to decide if a search would help given the chat history
DISABLE_LLM_CHOOSE_SEARCH = (
    os.environ.get("DISABLE_LLM_CHOOSE_SEARCH", "").lower() == "true"
)
DISABLE_LLM_QUERY_REPHRASE = (
    os.environ.get("DISABLE_LLM_QUERY_REPHRASE", "").lower() == "true"
)
# 1 edit per 20 characters, currently unused due to fuzzy match being too slow
QUOTE_ALLOWED_ERROR_PERCENT = 0.05
QA_TIMEOUT = int(os.environ.get("QA_TIMEOUT") or "60")  # 60 seconds
# Include additional document/chunk metadata in prompt to GenerativeAI
INCLUDE_METADATA = False
# Keyword Search Drop Stopwords
# If user has changed the default model, would most likely be to use a multilingual
# model, the stopwords are NLTK english stopwords so then we would want to not drop the keywords
if os.environ.get("EDIT_KEYWORD_QUERY"):
    EDIT_KEYWORD_QUERY = os.environ.get("EDIT_KEYWORD_QUERY", "").lower() == "true"
else:
    EDIT_KEYWORD_QUERY = not os.environ.get("DOCUMENT_ENCODER_MODEL")
# Weighting factor between Vector and Keyword Search, 1 for completely vector search
HYBRID_ALPHA = max(0, min(1, float(os.environ.get("HYBRID_ALPHA") or 0.62)))
# Weighting factor between Title and Content of documents during search, 1 for completely
# Title based. Default heavily favors Content because Title is also included at the top of
# Content. This is to avoid cases where the Content is very relevant but it may not be clear
# if the title is separated out. Title is most of a "boost" than a separate field.
TITLE_CONTENT_RATIO = max(
    0, min(1, float(os.environ.get("TITLE_CONTENT_RATIO") or 0.20))
)
# A list of languages passed to the LLM to rephase the query
# For example "English,French,Spanish", be sure to use the "," separator
MULTILINGUAL_QUERY_EXPANSION = os.environ.get("MULTILINGUAL_QUERY_EXPANSION") or None
LANGUAGE_HINT = "\n" + (
    os.environ.get("LANGUAGE_HINT")
    or "IMPORTANT: Respond in the same language as my query!"
)
LANGUAGE_CHAT_NAMING_HINT = (
    os.environ.get("LANGUAGE_CHAT_NAMING_HINT")
    or "The name of the conversation must be in the same language as the user query."
)

# Stops streaming answers back to the UI if this pattern is seen:
STOP_STREAM_PAT = os.environ.get("STOP_STREAM_PAT") or None

# The backend logic for this being True isn't fully supported yet
HARD_DELETE_CHATS = False
