"""End-to-end integration test for the per-persona multi-language flag.

Drives the real stack (Postgres + Vespa + your configured GenAI provider)
and verifies that:

  Phase 1  Seeded English docs land in Vespa (BM25 + embedding hits)
  Phase 2  A Persona with `multilingual_query_expansion=True` retrieves
           the seeded docs for non-English queries
  Phase 3  The streamed answer is in the user's original language
           (script-detection heuristic on Unicode ranges)
  Phase 4  A control Persona with the flag OFF behaves differently
           (logged, not asserted — flagged behavior is the contract)

Designed for a developer running the local stack. Uses the existing
ingestion + chat code paths directly (no HTTP) so it doubles as a
fast smoke test of the wiring we just added.

DESTRUCTIVE: writes (and on --clean removes) rows + Vespa documents
prefixed with `__test_multilang__`. Run only against a dev DB.

Usage:
    cd backend
    PYTHONPATH=$(pwd) python scripts/test_multilanguage_e2e.py [--yes] [--clean] [--keep-data]

Exits 0 on success, non-zero on the first hard failure. Phase 4 is
informational only and does not gate exit code.
"""
from __future__ import annotations

import argparse
import logging
import sys
import unicodedata
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.orm import Session

from danswer.chat.models import DanswerAnswerPiece
from danswer.chat.models import QADocsResponse
from danswer.chat.models import StreamingError
from danswer.chat.process_message import stream_chat_message_objects
from danswer.configs.constants import DocumentSource
from danswer.connectors.models import Document
from danswer.connectors.models import IndexAttemptMetadata
from danswer.connectors.models import InputType
from danswer.connectors.models import Section
from danswer.db.chat import create_chat_session
from danswer.db.chat import get_or_create_root_message
from danswer.db.embedding_model import get_current_db_embedding_model
from danswer.db.engine import get_session_context_manager
from danswer.db.engine import get_sqlalchemy_engine
from danswer.db.models import Connector
from danswer.db.models import ConnectorCredentialPair
from danswer.db.models import Credential
from danswer.db.models import Document as DbDocument
from danswer.db.models import DocumentByConnectorCredentialPair
from danswer.db.models import Persona
from danswer.db.models import Tool as ToolDBModel
from danswer.db.persona import get_default_prompt
from danswer.db.persona import upsert_persona
from danswer.tools.search.search_tool import SearchTool
from danswer.document_index.factory import get_default_document_index
from danswer.indexing.embedder import DefaultIndexingEmbedder
from danswer.indexing.indexing_pipeline import build_indexing_pipeline
from danswer.one_shot_answer.answer_question import get_search_answer
from danswer.one_shot_answer.models import DirectQARequest
from danswer.one_shot_answer.models import ThreadMessage
from danswer.search.enums import OptionalSearchSetting
from danswer.search.enums import RecencyBiasSetting
from danswer.search.models import RetrievalDetails
from danswer.server.query_and_chat.models import CreateChatMessageRequest


# Keep the global logger quiet so test output is readable.
logging.getLogger().setLevel(logging.WARNING)


SEED_PREFIX = "__test_multilang__"
PERSONA_ML_NAME = f"{SEED_PREFIX}persona-multilingual"
PERSONA_CONTROL_NAME = f"{SEED_PREFIX}persona-control"


# ---------------------------------------------------------------------------
# Seed corpus — three facts, each in a distinct doc, all in English.
# Designed so retrieval recall is unambiguous: each query maps cleanly
# to exactly one doc.
# ---------------------------------------------------------------------------


@dataclass
class SeedDoc:
    doc_id: str
    title: str
    body: str
    # The entity the query asks about (kept stable across translations
    # so we can verify the right doc was retrieved by checking the
    # answer's content for this string).
    expected_entity: str


# NOTE on entity naming: we deliberately use a fictitious-but-unique
# brand ("Zorblax") in seed docs so the queries do not collide with any
# real entity in the host's existing corpus (Salesforce accounts, Slack
# threads, etc.). When a generic name like "Acme Corp" is used, the
# retriever's history rephrase + multilingual translation can produce
# ambiguous fragments that match unrelated docs, and the answer LLM
# hedges. The unique brand keeps the right doc dominant.
SEED_CORPUS: list[SeedDoc] = [
    SeedDoc(
        doc_id=f"{SEED_PREFIX}doc-vacation-policy",
        title="Zorblax Vacation Policy",
        body=(
            "All Zorblax full-time employees are entitled to 25 paid "
            "vacation days per calendar year. Vacation days do not roll "
            "over to the following year. Requests must be submitted at "
            "least two weeks in advance through the Zorblax HR portal."
        ),
        expected_entity="25",
    ),
    SeedDoc(
        doc_id=f"{SEED_PREFIX}doc-vpn-setup",
        title="Zorblax VPN Setup Guide",
        body=(
            "To connect to the Zorblax VPN, install the GlobalProtect "
            "client from the IT self-service portal. Use your corporate "
            "email as the username and your single sign-on password. "
            "The Zorblax gateway URL is vpn.zorblax.example.com."
        ),
        expected_entity="GlobalProtect",
    ),
    SeedDoc(
        doc_id=f"{SEED_PREFIX}doc-printer-help",
        title="Zorblax Office Printer Troubleshooting",
        body=(
            "If the Zorblax office printer is not responding, first "
            "check the network cable and power. The Zorblax printer's "
            "IP address is 10.20.30.40. To reset the print queue, open "
            "the Printers control panel and select 'Cancel All "
            "Documents'."
        ),
        expected_entity="10.20.30.40",
    ),
]


# ---------------------------------------------------------------------------
# Test queries — each language asks the same questions about the seeded
# English docs. The translations are deliberately straightforward so the
# LLM rephrase has a fair chance.
# ---------------------------------------------------------------------------


@dataclass
class LanguageCase:
    code: str  # ISO-ish for display
    label: str
    queries: list[tuple[str, SeedDoc]]  # (query_text, expected_doc)


CASES: list[LanguageCase] = [
    LanguageCase(
        code="en",
        label="English",
        queries=[
            ("How many vacation days do Zorblax employees get?", SEED_CORPUS[0]),
            ("How do I connect to the Zorblax VPN?", SEED_CORPUS[1]),
            ("What is the IP address of the Zorblax office printer?", SEED_CORPUS[2]),
        ],
    ),
    LanguageCase(
        code="ja",
        label="Japanese",
        queries=[
            ("Zorblaxの従業員は何日の有給休暇が取れますか?", SEED_CORPUS[0]),
            ("ZorblaxのVPNに接続するにはどうすればいいですか?", SEED_CORPUS[1]),
            ("Zorblaxのオフィスプリンタの IPアドレスは何ですか?", SEED_CORPUS[2]),
        ],
    ),
    LanguageCase(
        code="zh",
        label="Chinese",
        queries=[
            ("Zorblax 公司的员工每年有多少天带薪休假?", SEED_CORPUS[0]),
            ("如何连接 Zorblax 公司的 VPN?", SEED_CORPUS[1]),
            ("Zorblax 办公室打印机的 IP 地址是多少?", SEED_CORPUS[2]),
        ],
    ),
    LanguageCase(
        code="ko",
        label="Korean",
        queries=[
            ("Zorblax 직원은 연간 며칠의 유급 휴가를 받을 수 있나요?", SEED_CORPUS[0]),
            ("Zorblax의 VPN에 어떻게 접속하나요?", SEED_CORPUS[1]),
            ("Zorblax 사무실 프린터의 IP 주소는 무엇인가요?", SEED_CORPUS[2]),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"
_INFO = "\033[33mINFO\033[0m"


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def ok(msg: str) -> None:
    print(f"  [{_PASS}] {msg}")


def fail(msg: str) -> None:
    print(f"  [{_FAIL}] {msg}")


def info(msg: str) -> None:
    print(f"  [{_INFO}] {msg}")


# ---------------------------------------------------------------------------
# Language detection — heuristic, by Unicode block dominance.
# ---------------------------------------------------------------------------


def detect_language(text: str) -> str:
    """Returns one of: 'ja', 'zh', 'ko', 'en', 'mixed/other'.

    Heuristic: count code points by script. If >= 5% Hiragana/Katakana,
    call it Japanese (kanji alone could be Japanese or Chinese, so
    presence of kana disambiguates). Else if >= 5% Hangul → Korean.
    Else if >= 5% CJK ideographs → Chinese. Else if mostly ASCII letters
    → English. Else 'mixed/other'.
    """
    if not text:
        return "mixed/other"
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
            # Latin-script letter (etc.)
            counts["ascii_letter"] += 1
            total_letters += 1
    if total_letters == 0:
        return "mixed/other"
    threshold = max(1, total_letters // 20)  # 5%
    if counts["hiragana_katakana"] >= threshold:
        return "ja"
    if counts["hangul"] >= threshold:
        return "ko"
    if counts["cjk"] >= threshold:
        return "zh"
    if counts["ascii_letter"] >= total_letters * 0.7:
        return "en"
    return "mixed/other"


# ---------------------------------------------------------------------------
# Setup: connector / credential / cc-pair / docs
# ---------------------------------------------------------------------------


def confirm_destructive(skip: bool) -> None:
    engine = get_sqlalchemy_engine()
    url = engine.url
    safe_url = f"{url.drivername}://{url.username}@{url.host}:{url.port}/{url.database}"
    if skip:
        print(f"[--yes] Proceeding against {safe_url}")
        return
    print(f"This script writes/deletes tagged ({SEED_PREFIX!r}) data in:")
    print(f"  {safe_url}")
    print("It also indexes a small set of test docs into Vespa.")
    answer = input("Type 'yes' to continue: ")
    if answer.strip().lower() != "yes":
        print("Aborted.")
        sys.exit(1)


def get_or_create_test_cc_pair(db: Session) -> ConnectorCredentialPair:
    existing = db.execute(
        select(ConnectorCredentialPair).where(
            ConnectorCredentialPair.name == f"{SEED_PREFIX}ccp"
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    connector = Connector(
        name=f"{SEED_PREFIX}connector",
        source=DocumentSource.INGESTION_API,
        input_type=InputType.LOAD_STATE,
        connector_specific_config={"_test_multilang": True},
        refresh_freq=None,
        disabled=False,
    )
    credential = Credential(admin_public=True, credential_json={})
    db.add_all([connector, credential])
    db.flush()
    ccp = ConnectorCredentialPair(
        connector_id=connector.id,
        credential_id=credential.id,
        name=f"{SEED_PREFIX}ccp",
        is_public=True,
        total_docs_indexed=0,
    )
    db.add(ccp)
    db.commit()
    return ccp


def seed_vespa_docs(db: Session, ccp: ConnectorCredentialPair) -> int:
    """Push the SEED_CORPUS through the real indexing pipeline so they
    land in Vespa with embeddings + BM25. Returns the number indexed."""
    embedding_model = get_current_db_embedding_model(db)
    document_index = get_default_document_index(
        primary_index_name=embedding_model.index_name,
        secondary_index_name=None,
    )

    embedder = DefaultIndexingEmbedder(
        model_name=embedding_model.model_name,
        normalize=embedding_model.normalize,
        query_prefix=embedding_model.query_prefix,
        passage_prefix=embedding_model.passage_prefix,
    )

    pipeline = build_indexing_pipeline(
        embedder=embedder,
        document_index=document_index,
        ignore_time_skip=True,
        db_session=db,
    )

    docs = [
        Document(
            id=sd.doc_id,
            sections=[Section(text=f"{sd.title}\n\n{sd.body}", link=None)],
            source=DocumentSource.INGESTION_API,
            semantic_identifier=sd.title,
            metadata={"_test_multilang": "true"},
            from_ingestion_api=True,
        )
        for sd in SEED_CORPUS
    ]

    new_doc, chunks = pipeline(
        documents=docs,
        index_attempt_metadata=IndexAttemptMetadata(
            connector_id=ccp.connector_id,
            credential_id=ccp.credential_id,
        ),
    )
    return new_doc


# ---------------------------------------------------------------------------
# Persona helpers
# ---------------------------------------------------------------------------


def upsert_test_persona(db: Session, name: str, multilingual: bool) -> Persona:
    default_prompt = get_default_prompt(db)
    # Without the SearchTool attached, the chat flow has nothing to
    # retrieve with — the LLM falls back to its training knowledge and
    # never sees our seeded docs. Look it up by in_code_tool_id so the
    # test isn't tied to a hardcoded id.
    search_tool_row = db.execute(
        select(ToolDBModel).where(ToolDBModel.in_code_tool_id == SearchTool.__name__)
    ).scalar_one_or_none()
    if search_tool_row is None:
        raise RuntimeError(
            "Built-in SearchTool not found in DB; ensure api-server has "
            "started at least once so it can seed in-code tools."
        )
    persona = upsert_persona(
        user=None,
        name=name,
        description=f"{SEED_PREFIX} persona for multilingual e2e test",
        num_chunks=10,
        llm_relevance_filter=False,
        llm_filter_extraction=False,
        recency_bias=RecencyBiasSetting.BASE_DECAY,
        llm_model_provider_override=None,
        llm_model_version_override=None,
        starter_messages=None,
        is_public=True,
        prompt_ids=[default_prompt.id],
        document_set_ids=[],
        tool_ids=[search_tool_row.id],
        multilingual_query_expansion=multilingual,
        db_session=db,
    )
    return persona


# ---------------------------------------------------------------------------
# Drive a single chat-message call and collect what we need.
# ---------------------------------------------------------------------------


@dataclass
class ChatProbeResult:
    answer_text: str
    retrieved_doc_ids: list[str]
    retrieved_titles: list[str]
    error: str | None


def probe_chat(persona: Persona, query: str) -> ChatProbeResult:
    """One-shot: create chat session, send message, drain the stream."""
    with get_session_context_manager() as db_session:
        chat_session = create_chat_session(
            db_session=db_session,
            description=f"{SEED_PREFIX}probe",
            user_id=None,
            persona_id=persona.id,
        )
        root = get_or_create_root_message(
            chat_session_id=chat_session.id, db_session=db_session
        )

        req = CreateChatMessageRequest(
            chat_session_id=chat_session.id,
            parent_message_id=root.id,
            message=query,
            file_descriptors=[],
            prompt_id=None,
            search_doc_ids=None,
            retrieval_options=RetrievalDetails(
                run_search=OptionalSearchSetting.ALWAYS, real_time=True
            ),
        )

        answer_pieces: list[str] = []
        retrieved_doc_ids: list[str] = []
        retrieved_titles: list[str] = []
        error: str | None = None

        try:
            for obj in stream_chat_message_objects(
                new_msg_req=req,
                user=None,
                db_session=db_session,
            ):
                if isinstance(obj, DanswerAnswerPiece):
                    if obj.answer_piece:
                        answer_pieces.append(obj.answer_piece)
                elif isinstance(obj, QADocsResponse):
                    for d in obj.top_documents or []:
                        retrieved_doc_ids.append(d.document_id)
                        retrieved_titles.append(d.semantic_identifier or "")
                elif isinstance(obj, StreamingError):
                    error = obj.error
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        return ChatProbeResult(
            answer_text="".join(answer_pieces).strip(),
            retrieved_doc_ids=retrieved_doc_ids,
            retrieved_titles=retrieved_titles,
            error=error,
        )


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup(db: Session) -> None:
    section("Cleanup")
    # FK dependency graph (collected via pg_constraint):
    #   chat_message__search_doc -> chat_message
    #   tool_call -> chat_message
    #   chat_feedback -> chat_message
    #   document_retrieval_feedback -> chat_message
    #   chat_message -> chat_session
    #   chat_session -> persona
    # We must drop dependents before parents. Use raw SQL — much
    # cleaner than walking the ORM for a destructive teardown.
    #
    # Match scope: any chat session whose description carries our
    # SEED_PREFIX *or* whose persona is one of our test personas.
    # That covers prior aborted runs, runs that crashed mid-test, and
    # the case where the chat UI was used to talk to our test persona.
    db.execute(
        text(
            """
            CREATE TEMP TABLE _ml_test_sessions ON COMMIT DROP AS
            SELECT cs.id
            FROM chat_session cs
            WHERE cs.description LIKE :prefix
               OR cs.persona_id IN (
                   SELECT id FROM persona WHERE name LIKE :prefix
               );
            """
        ),
        {"prefix": f"{SEED_PREFIX}%"},
    )
    db.execute(
        text(
            """
            CREATE TEMP TABLE _ml_test_messages ON COMMIT DROP AS
            SELECT id FROM chat_message
            WHERE chat_session_id IN (SELECT id FROM _ml_test_sessions);
            """
        )
    )

    deleted_msdoc = db.execute(
        text(
            """
            DELETE FROM chat_message__search_doc
            WHERE chat_message_id IN (SELECT id FROM _ml_test_messages);
            """
        )
    ).rowcount
    deleted_toolcall = db.execute(
        text(
            """
            DELETE FROM tool_call
            WHERE message_id IN (SELECT id FROM _ml_test_messages);
            """
        )
    ).rowcount
    deleted_cfeedback = db.execute(
        text(
            """
            DELETE FROM chat_feedback
            WHERE chat_message_id IN (SELECT id FROM _ml_test_messages);
            """
        )
    ).rowcount
    deleted_drfeedback = db.execute(
        text(
            """
            DELETE FROM document_retrieval_feedback
            WHERE chat_message_id IN (SELECT id FROM _ml_test_messages);
            """
        )
    ).rowcount
    deleted_msgs = db.execute(
        text(
            """
            DELETE FROM chat_message
            WHERE id IN (SELECT id FROM _ml_test_messages);
            """
        )
    ).rowcount
    deleted_sessions = db.execute(
        text(
            """
            DELETE FROM chat_session
            WHERE id IN (SELECT id FROM _ml_test_sessions);
            """
        )
    ).rowcount

    info(
        f"deleted {deleted_sessions} chat session(s), {deleted_msgs} "
        f"message(s); cascaded: msg__search_doc={deleted_msdoc}, "
        f"tool_call={deleted_toolcall}, chat_feedback={deleted_cfeedback}, "
        f"document_retrieval_feedback={deleted_drfeedback}"
    )

    # Personas (now safe to drop — no chat session points at them).
    personas = (
        db.execute(select(Persona).where(Persona.name.like(f"{SEED_PREFIX}%")))
        .scalars()
        .all()
    )
    for p in personas:
        db.delete(p)
    info(f"deleted {len(personas)} test persona(s)")

    # Documents (Postgres rows; Vespa cleanup is best-effort below)
    db_docs = (
        db.execute(select(DbDocument).where(DbDocument.id.like(f"{SEED_PREFIX}%")))
        .scalars()
        .all()
    )
    for d in db_docs:
        db.execute(
            DocumentByConnectorCredentialPair.__table__.delete().where(
                DocumentByConnectorCredentialPair.id == d.id
            )
        )
        db.delete(d)
    info(f"deleted {len(db_docs)} document row(s)")

    # cc-pair, connector, credential
    ccp = db.execute(
        select(ConnectorCredentialPair).where(
            ConnectorCredentialPair.name == f"{SEED_PREFIX}ccp"
        )
    ).scalar_one_or_none()
    if ccp is not None:
        connector_id = ccp.connector_id
        credential_id = ccp.credential_id
        db.delete(ccp)
        connector = db.get(Connector, connector_id)
        if connector is not None:
            db.delete(connector)
        credential = db.get(Credential, credential_id)
        if credential is not None:
            db.delete(credential)
        info("deleted test cc-pair / connector / credential")

    db.commit()
    info(
        "Vespa: tagged test docs intentionally left in the index "
        "(deletion goes through the connector framework). Re-running "
        "this test reindexes them in place."
    )


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


def phase_setup(db: Session) -> tuple[ConnectorCredentialPair, Persona, Persona]:
    section("Phase 1 — setup test fixtures")
    ccp = get_or_create_test_cc_pair(db)
    ok(f"cc-pair {ccp.id} ({ccp.name}) ready")
    n_indexed = seed_vespa_docs(db, ccp)
    if n_indexed != len(SEED_CORPUS):
        # n_indexed is the count of *new* docs, so a re-run yields 0.
        info(
            f"indexing pipeline reported {n_indexed} new docs "
            f"(re-runs reindex existing docs in place)"
        )
    ok(f"seeded {len(SEED_CORPUS)} English doc(s) into Vespa")

    persona_ml = upsert_test_persona(db, PERSONA_ML_NAME, multilingual=True)
    persona_ctrl = upsert_test_persona(db, PERSONA_CONTROL_NAME, multilingual=False)
    ok(
        f"persona [{persona_ml.name}] id={persona_ml.id}, "
        f"multilingual_query_expansion={persona_ml.multilingual_query_expansion}"
    )
    ok(
        f"persona [{persona_ctrl.name}] id={persona_ctrl.id}, "
        f"multilingual_query_expansion={persona_ctrl.multilingual_query_expansion}"
    )
    return ccp, persona_ml, persona_ctrl


def phase_english_baseline(persona_ml: Persona) -> bool:
    section("Phase 2 — English baseline (sanity check)")
    case = next(c for c in CASES if c.code == "en")
    failures = 0
    for query, expected_doc in case.queries:
        result = probe_chat(persona_ml, query)
        if result.error:
            fail(f"[en] '{query[:60]}' streaming error: {result.error}")
            failures += 1
            continue
        if expected_doc.doc_id in result.retrieved_doc_ids:
            ok(f"[en] retrieval hit expected doc for: '{query[:60]}'")
        else:
            fail(f"[en] expected doc NOT in top docs for: '{query[:60]}'")
            info(f"      retrieved: {result.retrieved_titles[:3]}")
            failures += 1
        # Sanity: did the expected entity appear in the answer?
        if expected_doc.expected_entity.lower() in result.answer_text.lower():
            ok(
                f"[en] answer contains expected entity "
                f"'{expected_doc.expected_entity}'"
            )
        else:
            info(
                f"[en] answer does NOT contain '{expected_doc.expected_entity}' "
                f"(LLM may have paraphrased; check manually). "
                f"Answer head: {result.answer_text[:120]!r}"
            )
    return failures == 0


def phase_non_english(persona_ml: Persona) -> bool:
    """Hard contract for the persona flag: when on, non-English queries
    must (a) translate-for-retrieval so the right English doc is found,
    (b) the answer must contain the factual entity from that doc
    (numeric / proper-noun entities survive translation), AND (c) the
    final answer text is in the user's language. (c) is enforced by the
    post-translation pass in process_message.py — the answering LLM
    might still produce English internally, but the second pass
    translates that to the user's language before we yield it."""
    section("Phase 3 — non-English queries with multilingual flag ON")
    failures = 0
    lang_match = 0
    lang_total = 0
    for case in CASES:
        if case.code == "en":
            continue
        for query, expected_doc in case.queries:
            result = probe_chat(persona_ml, query)
            if result.error:
                fail(
                    f"[{case.code}] '{query[:60]}' streaming error: " f"{result.error}"
                )
                failures += 1
                continue

            # 3a — retrieval brought back the right English doc.
            # This proves the persona flag wired translate-to-English
            # into retrieval.
            if expected_doc.doc_id in result.retrieved_doc_ids:
                ok(f"[{case.code}] retrieval hit expected doc for: " f"'{query[:60]}'")
            else:
                fail(
                    f"[{case.code}] expected doc NOT in top docs for: "
                    f"'{query[:60]}'"
                )
                info(f"      retrieved: {result.retrieved_titles[:3]}")
                failures += 1

            # 3b — answer contains the expected entity. Entities are
            # numerals / proper nouns that survive translation, so the
            # LLM should keep them verbatim regardless of output
            # language. This is the strongest correctness signal.
            if expected_doc.expected_entity.lower() in result.answer_text.lower():
                ok(
                    f"[{case.code}] answer contains expected entity "
                    f"'{expected_doc.expected_entity}'"
                )
            else:
                fail(
                    f"[{case.code}] answer missing entity "
                    f"'{expected_doc.expected_entity}'. Answer head: "
                    f"{result.answer_text[:120]!r}"
                )
                failures += 1

            # 3c — answer is in the user's language. Now a hard
            # assertion because the post-translation pass guarantees
            # this regardless of the answering LLM's behavior. If the
            # detected language doesn't match, either the post-pass
            # was not invoked (wiring bug) or it returned the English
            # fallback (translate LLM call failed).
            detected = detect_language(result.answer_text)
            lang_total += 1
            if detected == case.code:
                lang_match += 1
                ok(f"[{case.code}] answer language: {detected}")
            else:
                fail(
                    f"[{case.code}] expected {case.code} answer, detected "
                    f"{detected}. Answer head: {result.answer_text[:200]!r}"
                )
                failures += 1
    info(
        f"language-match summary: {lang_match}/{lang_total} non-English "
        f"answers came back in the user's language"
    )
    return failures == 0


def probe_slack(persona: Persona, query: str) -> ChatProbeResult:
    """Drive the one-shot answer path that the Slack listener uses.
    `get_search_answer` runs the same Answer pipeline as chat but with
    its own retry loop and citation enforcement."""
    with get_session_context_manager() as db_session:
        req = DirectQARequest(
            messages=[ThreadMessage(message=query, sender=None)],
            prompt_id=None,
            persona_id=persona.id,
            retrieval_options=RetrievalDetails(
                run_search=OptionalSearchSetting.ALWAYS, real_time=True
            ),
        )
        try:
            response = get_search_answer(
                query_req=req,
                user=None,
                max_document_tokens=None,
                max_history_tokens=None,
                db_session=db_session,
                use_citations=True,
                danswerbot_flow=True,
            )
        except Exception as exc:
            return ChatProbeResult(
                answer_text="",
                retrieved_doc_ids=[],
                retrieved_titles=[],
                error=f"{type(exc).__name__}: {exc}",
            )

        retrieved_doc_ids: list[str] = []
        retrieved_titles: list[str] = []
        if response.docs and response.docs.top_documents:
            for d in response.docs.top_documents:
                retrieved_doc_ids.append(d.document_id)
                retrieved_titles.append(d.semantic_identifier or "")
        return ChatProbeResult(
            answer_text=(response.answer or "").strip(),
            retrieved_doc_ids=retrieved_doc_ids,
            retrieved_titles=retrieved_titles,
            error=response.error_msg,
        )


def phase_slack(persona_ml: Persona) -> bool:
    """Smoke test for the Slack one-shot path. Same hard contract as
    Phase 3 (retrieval hit + entity in answer + answer in user's
    language), but driven through `get_search_answer` — the function
    the slack listener calls."""
    section("Phase 5 — Slack one-shot path with multilingual flag ON")
    failures = 0
    lang_match = 0
    lang_total = 0
    for case in CASES:
        if case.code == "en":
            continue
        # One query per language is plenty for a smoke test (each
        # query takes 2× LLM round-trips: answer + translate).
        query, expected_doc = case.queries[0]
        result = probe_slack(persona_ml, query)
        if result.error:
            fail(f"[slack {case.code}] '{query[:60]}' error: {result.error}")
            failures += 1
            continue

        if expected_doc.doc_id in result.retrieved_doc_ids:
            ok(f"[slack {case.code}] retrieval hit expected doc")
        else:
            fail(
                f"[slack {case.code}] expected doc NOT in top docs. "
                f"retrieved: {result.retrieved_titles[:3]}"
            )
            failures += 1

        if expected_doc.expected_entity.lower() in result.answer_text.lower():
            ok(
                f"[slack {case.code}] answer contains entity "
                f"'{expected_doc.expected_entity}'"
            )
        else:
            fail(
                f"[slack {case.code}] answer missing entity "
                f"'{expected_doc.expected_entity}'. Answer head: "
                f"{result.answer_text[:120]!r}"
            )
            failures += 1

        detected = detect_language(result.answer_text)
        lang_total += 1
        if detected == case.code:
            lang_match += 1
            ok(f"[slack {case.code}] answer language: {detected}")
        else:
            fail(
                f"[slack {case.code}] expected {case.code}, detected "
                f"{detected}. Answer head: {result.answer_text[:200]!r}"
            )
            failures += 1
    info(
        f"slack-path language-match summary: {lang_match}/{lang_total} "
        f"non-English answers came back in the user's language"
    )
    return failures == 0


def phase_control(persona_ctrl: Persona) -> None:
    section("Phase 4 — control: same queries with flag OFF (informational)")
    for case in CASES:
        if case.code == "en":
            continue
        # Just one query per language is enough to see the contrast.
        query, expected_doc = case.queries[0]
        result = probe_chat(persona_ctrl, query)
        if result.error:
            info(f"[{case.code}] streaming error: {result.error}")
            continue
        retrieved = expected_doc.doc_id in result.retrieved_doc_ids
        detected = detect_language(result.answer_text)
        info(
            f"[{case.code}] flag-OFF persona | retrieval-hit={retrieved} | "
            f"answer-lang={detected}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the destructive-action confirmation prompt",
    )
    parser.add_argument(
        "--clean", action="store_true", help="Remove tagged test data and exit"
    )
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="Skip cleanup at the end of a successful run",
    )
    args = parser.parse_args()

    confirm_destructive(args.yes)

    if args.clean:
        with get_session_context_manager() as db:
            cleanup(db)
        return 0

    overall_ok = True
    with get_session_context_manager() as db:
        try:
            ccp, persona_ml, persona_ctrl = phase_setup(db)
        except Exception as exc:
            fail(f"setup failed: {type(exc).__name__}: {exc}")
            return 2

    if not phase_english_baseline(persona_ml):
        overall_ok = False

    if not phase_non_english(persona_ml):
        overall_ok = False

    if not phase_slack(persona_ml):
        overall_ok = False

    phase_control(persona_ctrl)

    if not args.keep_data:
        with get_session_context_manager() as db:
            cleanup(db)

    print()
    if overall_ok:
        print(f"[{_PASS}] multi-language e2e: all hard assertions passed")
        return 0
    print(f"[{_FAIL}] multi-language e2e: see failures above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
