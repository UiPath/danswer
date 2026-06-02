"""Seed N varied personas/assistants into the local DB for UX testing.

WARNING — local dev tool only. Runs whatever `DATABASE_URL` / `POSTGRES_*`
env vars point at. NEVER point this at a prod Postgres. If `POSTGRES_HOST`
contains anything that smells like prod (configured to error below), the
script aborts.

Produces a realistic mix for exercising the redesigned gallery page:

  ~30%  "Yours"           — owned by the target user (private)
  ~20%  "Shared with you" — owned by another user, target user in users[]
   ~50% "Featured"        — public (is_public=True, no specific owner)

Each row gets a random subset of available tools / document sets so the
{n} tools / {n} sources chips render with variety. Half of "Yours" land
in the user's chosen_assistants picker, half do not — so the "Already
added" / "Available to add" filter chips have content on both sides.

Usage (from repo root):

    cd backend
    source ../.venv/bin/activate
    python -m scripts.seed_assistants --email you@example.com --count 50

    # Wipe just the seeded rows (by name prefix) and re-seed:
    python -m scripts.seed_assistants --clear
    python -m scripts.seed_assistants --email you@example.com --count 50

Notes:
  * Re-running without --clear stacks more rows. Use --prefix to namespace.
  * If --email isn't supplied, picks the first admin user in the DB.
  * If only one user exists, the "Shared with you" tier is folded into
    "Featured" since there's no one else to own them.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from danswer.auth.schemas import UserRole
from danswer.db.engine import SessionFactory
from danswer.db.models import DocumentSet
from danswer.db.models import Persona
from danswer.db.models import Persona__User
from danswer.db.models import Tool
from danswer.db.models import User
from danswer.search.enums import RecencyBiasSetting


# --- safety: don't blast prod by accident ---------------------------------

# If POSTGRES_HOST contains any of these substrings, bail. Extend as
# needed. The whole point is: this script generates fake data; you only
# want it on your own laptop's Postgres.
_PROD_HOST_FINGERPRINTS = (
    "azure.com",  # Azure managed Postgres (darwin uses one)
    "amazonaws.com",
    "rds.",
    "gcp.",
    ".cloud.",
    "prod",
    "production",
)


def _abort_if_pointed_at_prod() -> None:
    host = (os.environ.get("POSTGRES_HOST") or "").lower()
    for marker in _PROD_HOST_FINGERPRINTS:
        if marker in host:
            print(
                f"REFUSING TO RUN: POSTGRES_HOST={host!r} looks like a prod DB.\n"
                f"Point POSTGRES_HOST at localhost / your dev container first.",
                file=sys.stderr,
            )
            sys.exit(2)


# --- content pools --------------------------------------------------------

# 60 distinct names so we can cover the requested ~50 without dup.
_NAMES: list[str] = [
    "Research Pal",
    "Code Reviewer",
    "SQL Helper",
    "Email Drafter",
    "Bug Triage",
    "API Documenter",
    "Test Writer",
    "Meeting Summarizer",
    "Slack Digest",
    "Stand-up Buddy",
    "Customer Insights",
    "Onboarding Guide",
    "Roadmap Reviewer",
    "Incident Reporter",
    "Refactor Assistant",
    "Release Notes",
    "Spec Reader",
    "RFC Writer",
    "PR Summarizer",
    "Postmortem Helper",
    "Design Critic",
    "Architecture Sketch",
    "Security Reviewer",
    "Threat Modeler",
    "Compliance Auditor",
    "Pricing Analyst",
    "Sales Enabler",
    "Renewal Scout",
    "Churn Predictor",
    "Marketing Riff",
    "Blog Draftsman",
    "Tweet Polisher",
    "Tagline Brewer",
    "FAQ Generator",
    "Support Tier-1",
    "Escalation Helper",
    "Runbook Walker",
    "Migration Planner",
    "Schema Diff Reader",
    "Index Tuner",
    "Query Explainer",
    "Log Whisperer",
    "Metric Hunter",
    "Alert Wrangler",
    "Dashboard Builder",
    "Hire Brief",
    "Interview Scribe",
    "Skill Mapper",
    "Doc Search",
    "Wiki Pal",
    "Note Taker",
    "Action-Items Finder",
    "Standup Cliff-Notes",
    "Investor FAQ",
    "Roadblock Spotter",
    "OKR Reviewer",
    "Quarterly Recap",
    "Pitch Sharpener",
    "Customer-Reply Drafter",
    "Demo Outline",
]

# 30 description templates — varied tones / scopes so the cards don't all
# read the same.
_DESCRIPTIONS: list[str] = [
    "Answers questions about our codebase using semantic search across the indexed repos.",
    "Drafts polished customer-facing emails in the company's voice.",
    "Summarizes long Slack threads and surfaces decisions and action items.",
    "Reads design docs and points out the assumptions and the risky bits.",
    "Generates SQL against the analytics warehouse from a plain-English question.",
    "Triages new bug reports — classifies severity, finds duplicates, and assigns.",
    "Writes release notes from a list of merged PR titles.",
    "Cross-references Jira tickets and surfaces blocked dependencies.",
    "Helps onboard new engineers by answering 'where does X live?' questions.",
    "Reviews pull requests for naming, structure, and style consistency.",
    "Drafts incident postmortems from log excerpts and timeline notes.",
    "Translates marketing copy into different audience voices.",
    "Walks runbooks step by step, asking before each destructive action.",
    "Reads the customer-success knowledge base and answers tier-1 tickets.",
    "Explains an unfamiliar SQL query — joins, CTEs, window functions.",
    "Reviews quarterly OKR drafts for measurability and ambition.",
    "Builds the outline of a sales demo from a list of pain points.",
    "Tightens taglines — shorter, sharper, fewer adjectives.",
    "Sketches an architecture diagram outline from a design doc.",
    "Surfaces churn-risk signals from a list of recent customer emails.",
    "Answers HR / benefits FAQ from the employee handbook.",
    "Reads RFCs and writes the executive summary at the top.",
    "Indexes API documentation and answers 'how do I do X' questions.",
    "Drafts response templates for support tickets matching common patterns.",
    "Generates test cases for a function or endpoint from its signature.",
    "Reviews threat models against OWASP top-10 categories.",
    "Plans data migrations — pre-checks, batch sizing, rollback steps.",
    "Reads incident-channel logs and produces a concise five-line summary.",
    "Brainstorms blog post angles given a working title.",
    "Helps interviewers stay structured — drafts notes, scores, follow-ups.",
]


# --- helpers --------------------------------------------------------------


def _pick(rng: random.Random, items: Sequence, k_min: int, k_max: int) -> list:
    """Return between k_min and k_max random items (without replacement).

    Tolerates `items` being shorter than k_max — caps at available length.
    """
    if not items:
        return []
    upper = min(k_max, len(items))
    k = rng.randint(k_min, upper)
    if k <= 0:
        return []
    return rng.sample(list(items), k)


def _resolve_target_user(session: Session, email: str | None) -> User | None:
    if email:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"No user with email {email!r} found.", file=sys.stderr)
        return user
    # No email given — prefer an admin user, fall back to any user.
    admin = session.scalar(select(User).where(User.role == UserRole.ADMIN).limit(1))
    if admin is not None:
        return admin
    return session.scalar(select(User).limit(1))


def _pick_other_user(session: Session, target_user_id) -> User | None:
    """Find a user other than the target to own the "shared with you" rows."""
    return session.scalar(select(User).where(User.id != target_user_id).limit(1))


def _clear(session: Session, prefix: str) -> int:
    """Soft-delete by name prefix is risky if a real persona shares the
    prefix. We assert prefix is non-empty and unmistakably synthetic.
    """
    if not prefix or len(prefix) < 3:
        print(
            f"Refusing to clear with suspiciously short prefix {prefix!r}.",
            file=sys.stderr,
        )
        sys.exit(2)
    personas = session.scalars(
        select(Persona).where(Persona.name.startswith(prefix))
    ).all()
    n = 0
    for p in personas:
        # Hard delete — these are synthetic seed rows, not user data.
        # Junction rows clean up via cascade configured on the model.
        session.delete(p)
        n += 1
    session.commit()
    return n


# --- main -----------------------------------------------------------------


def main() -> None:
    _abort_if_pointed_at_prod()

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--count", type=int, default=50, help="How many to create.")
    ap.add_argument(
        "--email",
        help="Target user email — the 'me' for testing. Default: first admin user.",
    )
    ap.add_argument(
        "--prefix",
        default="[seed] ",
        help="Name prefix so seeded rows are easy to spot / clear. (default: '[seed] ')",
    )
    ap.add_argument(
        "--clear",
        action="store_true",
        help="Delete previously seeded personas (by --prefix) and exit.",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed — same seed = same data each run. Default: 42.",
    )
    args = ap.parse_args()

    with SessionFactory() as session:
        if args.clear:
            n = _clear(session, args.prefix)
            print(f"Cleared {n} seeded personas (prefix={args.prefix!r}).")
            return

        target_user = _resolve_target_user(session, args.email)
        if target_user is None:
            print(
                "No users in DB. Sign in to the app first so a user row "
                "exists, then re-run.",
                file=sys.stderr,
            )
            sys.exit(1)

        other_user = _pick_other_user(session, target_user.id)
        tools = list(session.scalars(select(Tool)).all())
        doc_sets = list(session.scalars(select(DocumentSet)).all())

        rng = random.Random(args.seed)

        if args.count > len(_NAMES):
            print(
                f"--count={args.count} exceeds {len(_NAMES)} unique names; "
                f"will cycle with numeric suffixes.",
                file=sys.stderr,
            )

        # Yours: ~30%, Shared: ~20% (only if other_user exists), rest Featured.
        yours_n = max(1, args.count * 30 // 100)
        shared_n = args.count * 20 // 100 if other_user is not None else 0
        featured_n = args.count - yours_n - shared_n

        # Track which Yours rows land in the user's picker (half do).
        # We'll mutate chosen_assistants at the end of the run.
        new_chosen_ids: list[int] = []

        created = 0
        for i in range(args.count):
            base_name = _NAMES[i % len(_NAMES)]
            suffix = "" if i < len(_NAMES) else f" #{i // len(_NAMES) + 1}"
            name = f"{args.prefix}{base_name}{suffix}"
            desc = rng.choice(_DESCRIPTIONS)
            persona_tools = _pick(rng, tools, 0, 3)
            persona_docs = _pick(rng, doc_sets, 0, 2)

            if i < yours_n:
                owner_id = target_user.id
                is_public = False
                shared_target = None
            elif i < yours_n + shared_n:
                # Owned by someone else, granted to target user via Persona__User.
                owner_id = other_user.id if other_user else None
                is_public = False
                shared_target = target_user.id
            else:
                # Public / featured — no specific owner.
                owner_id = None
                is_public = True
                shared_target = None

            persona = Persona(
                name=name,
                description=desc,
                user_id=owner_id,
                is_public=is_public,
                # Required scalars on Persona — pick sensible defaults so
                # the row is queryable by get_personas without errors.
                llm_relevance_filter=False,
                llm_filter_extraction=False,
                recency_bias=RecencyBiasSetting.AUTO,
                default_persona=False,
                is_visible=True,
                deleted=False,
                num_chunks=None,
                llm_model_provider_override=None,
                llm_model_version_override=None,
                starter_messages=None,
                tools=persona_tools,
                document_sets=persona_docs,
            )
            session.add(persona)
            session.flush()  # populate persona.id

            if shared_target is not None:
                session.add(Persona__User(persona_id=persona.id, user_id=shared_target))

            # Half of "Yours" auto-land in the picker; the other half are
            # available-to-add. Featured rows never auto-add (the user can
            # add them from the gallery). Shared rows auto-add so the user
            # sees their permitted assistants in chat immediately.
            if i < yours_n and i % 2 == 0:
                new_chosen_ids.append(persona.id)
            elif yours_n <= i < yours_n + shared_n:
                new_chosen_ids.append(persona.id)

            created += 1

        # Merge with the target user's existing chosen_assistants (if any).
        # We APPEND so we don't disturb whatever order they already have.
        if new_chosen_ids:
            existing = list(target_user.chosen_assistants or [])
            target_user.chosen_assistants = existing + new_chosen_ids

        session.commit()

        print(f"Created {created} personas under prefix {args.prefix!r}.")
        print(f"  Target user        : {target_user.email}")
        if other_user is not None:
            print(f"  Shared-from user   : {other_user.email}")
        print(f"  Yours              : {yours_n}")
        print(f"  Shared with you    : {shared_n}")
        print(f"  Featured / public  : {featured_n}")
        print(f"  Auto-added to picker: {len(new_chosen_ids)}")
        print()
        print("Open /assistants/gallery to see them. Run with --clear to wipe.")


if __name__ == "__main__":
    main()
