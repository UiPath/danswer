"""Helpers for the per-persona multi-language post-processing pass.

When a persona has `multilingual_query_expansion=True` and the user's
query is non-English, the answering LLM still produces English most of
the time (it tends to mirror the English context corpus regardless of
the LANGUAGE_HINT directive). We compensate by post-translating the
English answer back into the user's original language.

Trade-off: in translate mode we buffer the streamed answer instead of
showing it token-by-token. The user sees a brief delay (one extra LLM
round-trip), but reliably gets a reply in their language. English
queries are unaffected — they keep streaming normally.
"""
from __future__ import annotations

import unicodedata

from danswer.llm.interfaces import LLM
from danswer.llm.utils import dict_based_prompt_to_langchain_prompt
from danswer.llm.utils import message_to_string
from danswer.utils.logger import setup_logger

logger = setup_logger()


# Display name passed to the translation prompt. Keys are the language
# codes detect_query_language returns. Anything not in this map is
# treated as English (no translation needed).
_LANGUAGE_NAMES: dict[str, str] = {
    "ja": "Japanese",
    "zh": "Chinese (Simplified)",
    "ko": "Korean",
}


def detect_query_language(text: str) -> str:
    """Cheap script-based language detector covering the languages we
    explicitly support translation for. Returns one of: 'ja', 'zh',
    'ko', or 'en' (English/other — no translation needed).

    Heuristic mirrors the script-presence test in
    backend/scripts/test_multilanguage_e2e.py: a few percent of CJK /
    Hangul / kana code points is enough to decide. We don't try to be
    clever about mixed-language queries — the dominant non-English
    script wins, and ties default to English.
    """
    if not text:
        return "en"

    counts = {"hiragana_katakana": 0, "hangul": 0, "cjk": 0, "ascii_letter": 0}
    total_letters = 0
    for ch in text:
        cp = ord(ch)
        if (0x3040 <= cp <= 0x309F) or (0x30A0 <= cp <= 0x30FF):
            counts["hiragana_katakana"] += 1
            total_letters += 1
        elif 0xAC00 <= cp <= 0xD7AF:
            counts["hangul"] += 1
            total_letters += 1
        elif (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF):
            counts["cjk"] += 1
            total_letters += 1
        elif unicodedata.category(ch).startswith("L"):
            counts["ascii_letter"] += 1
            total_letters += 1

    if total_letters == 0:
        return "en"
    threshold = max(1, total_letters // 20)  # ~5%
    if counts["hiragana_katakana"] >= threshold:
        return "ja"
    if counts["hangul"] >= threshold:
        return "ko"
    if counts["cjk"] >= threshold:
        return "zh"
    return "en"


def language_name(code: str) -> str | None:
    return _LANGUAGE_NAMES.get(code)


# The prompt is intentionally directive about preserving citations and
# not adding commentary. Citations are bracketed numerals like [1] /
# [[1]](url); URLs and code blocks should also pass through unchanged.
_TRANSLATE_PROMPT = """\
You are a precise translator.

Translate the text below into {target_language}.

CRITICAL RULES — follow exactly:
- Preserve every citation marker exactly as-is. Citation markers look
  like [1], [2], [[1]](https://example.com), etc. Do not translate
  them, do not change the brackets, do not change the numbers.
- Preserve every URL exactly.
- Preserve every code block (text between triple backticks) exactly.
- Preserve every inline code span (text between single backticks).
- Do not add any commentary, preface, or trailing notes — output only
  the translated text.
- Keep numbers, proper nouns, and product names in their original
  form unless the target language has a well-established equivalent.

TEXT TO TRANSLATE:
{text}
"""


def translate_answer_to_language(
    answer_text: str,
    target_language_code: str,
    llm: LLM,
) -> str:
    """Translate `answer_text` into the language named by
    `target_language_code` (a key of _LANGUAGE_NAMES). Returns the
    English original on any failure — better to ship an English answer
    than to drop the response entirely."""
    target_name = _LANGUAGE_NAMES.get(target_language_code)
    if target_name is None:
        # Caller should have skipped, but be defensive.
        return answer_text

    if not answer_text.strip():
        return answer_text

    prompt_messages = [
        {
            "role": "user",
            "content": _TRANSLATE_PROMPT.format(
                target_language=target_name, text=answer_text
            ),
        }
    ]

    try:
        filled = dict_based_prompt_to_langchain_prompt(prompt_messages)
        translated = message_to_string(llm.invoke(filled))
    except Exception:
        logger.exception(
            "Failed to translate answer to %s; falling back to English",
            target_name,
        )
        return answer_text

    translated = translated.strip()
    if not translated:
        logger.warning(
            "Translation to %s came back empty; falling back to English",
            target_name,
        )
        return answer_text
    return translated
