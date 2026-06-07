# The following prompts are used to pass each chunk to the LLM (the cheap/fast one)
# to determine if the chunk is useful towards the user query. This is used as part
# of the reranking flow

USEFUL_PAT = "Yes useful"
NONUSEFUL_PAT = "Not useful"
CHUNK_FILTER_PROMPT = f"""
Determine if the reference section is USEFUL for answering the user query.
It is NOT enough for the section to be related to the query, \
it must contain information that is USEFUL for answering the query.
If the section contains ANY useful information, that is good enough, \
it does not need to fully answer the every part of the user query.

Reference Section:
```
{{chunk_text}}
```

User Query:
```
{{user_query}}
```

Respond with EXACTLY AND ONLY: "{USEFUL_PAT}" or "{NONUSEFUL_PAT}"
""".strip()


# Listwise variant: judge ALL candidate sections in ONE call (cheaper +
# lower-latency than one call per chunk, and lets the model compare them).
# Run on the MAIN llm. {sections} is a numbered list; the model returns a JSON
# array of the USEFUL section numbers.
LISTWISE_CHUNK_FILTER_PROMPT = """
You are given {count} numbered reference sections and a user query. For EACH
section, decide whether it is USEFUL for answering the query. It is NOT enough
to be related — the section must contain information USEFUL for answering. If a
section contains ANY useful information that counts; it need not fully answer
the query.

Reference Sections:
{sections}

User Query:
```
{user_query}
```

Respond with EXACTLY AND ONLY a JSON array of the numbers of the useful
sections, in any order, e.g. [1, 3, 4]. If none are useful, respond with [].
""".strip()


# Use the following for easy viewing of prompts
if __name__ == "__main__":
    print(CHUNK_FILTER_PROMPT)
    print("\n\n")
    print(LISTWISE_CHUNK_FILTER_PROMPT)
