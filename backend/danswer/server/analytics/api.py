"""Admin analytics endpoints (community / open-source).

Lives in `danswer.server.analytics`, parallel to (not depending on) the
EE module at `ee.danswer.server.analytics`. Same routes (`/analytics/...`)
and same response shapes — the frontend can target either backend
without code changes.

If the EE build also registers its analytics router, FastAPI ends up
with duplicate routes; the community one is registered first by
`danswer.main:get_application` and wins. Removing the EE registration
is the cleanest way to avoid that ambiguity, but it's not required for
correctness.
"""
import datetime

from fastapi import APIRouter
from fastapi import Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

import danswer.db.models as db_models
from danswer.auth.users import current_admin_user
from danswer.db.analytics import fetch_docs_per_source
from danswer.db.analytics import fetch_document_set_usage
from danswer.db.analytics import fetch_per_user_chat_stats
from danswer.db.analytics import fetch_persona_usage
from danswer.db.analytics import fetch_slack_bot_channel_stats
from danswer.db.analytics import fetch_total_docs_indexed
from danswer.db.analytics import fetch_user_adoption
from danswer.db.analytics_rollup import fetch_danswerbot_analytics_from_rollup
from danswer.db.analytics_rollup import fetch_query_analytics_from_rollup
from danswer.db.analytics_rollup import fetch_user_analytics_from_rollup
from danswer.db.engine import get_session

router = APIRouter(prefix="/analytics")


class QueryAnalyticsResponse(BaseModel):
    total_queries: int
    total_likes: int
    total_dislikes: int
    # Slackbot resolved-button presses (predefined_feedback='resolved').
    # Counted as a positive signal alongside likes for "strict NPS".
    total_resolved: int
    # Slackbot "I need more help" presses (required_followup=true). Counted
    # as a negative signal alongside dislikes for "strict NPS".
    total_needs_help: int
    date: datetime.date


@router.get("/admin/query")
def get_query_analytics(
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    _: db_models.User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> list[QueryAnalyticsResponse]:
    """Daily query volume + feedback breakdown. Default window: last 30 days.

    Reads from `analytics_daily_rollup`, populated nightly at 07:30 UTC.
    Days that haven't been rolled up yet (e.g. today before 07:30) are
    simply absent from the response.
    """
    daily_query_usage_info = fetch_query_analytics_from_rollup(
        start=start or (datetime.datetime.utcnow() - datetime.timedelta(days=30)),
        end=end or datetime.datetime.utcnow(),
        db_session=db_session,
    )
    return [
        QueryAnalyticsResponse(
            total_queries=total_queries,
            total_likes=total_likes,
            total_dislikes=total_dislikes,
            total_resolved=total_resolved,
            total_needs_help=total_needs_help,
            date=date,
        )
        for (
            total_queries,
            total_likes,
            total_dislikes,
            total_resolved,
            total_needs_help,
            date,
        ) in daily_query_usage_info
    ]


class UserAnalyticsResponse(BaseModel):
    total_active_users: int
    date: datetime.date


@router.get("/admin/user")
def get_user_analytics(
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    _: db_models.User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> list[UserAnalyticsResponse]:
    """Distinct active users per day, served from `analytics_daily_rollup`."""
    rows = fetch_user_analytics_from_rollup(
        start=start or (datetime.datetime.utcnow() - datetime.timedelta(days=30)),
        end=end or datetime.datetime.utcnow(),
        db_session=db_session,
    )
    return [
        UserAnalyticsResponse(total_active_users=int(active_users), date=date)
        for active_users, date in rows
    ]


class UserAdoptionResponse(BaseModel):
    # Users who first used chat on this date.
    new_users: int
    # Running total of distinct users who had ever used chat as of this date.
    cumulative_users: int
    date: datetime.date


@router.get("/admin/user-adoption")
def get_user_adoption_analytics(
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    _: db_models.User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> list[UserAdoptionResponse]:
    """Chat adoption curve: new + cumulative distinct users per day, served
    from the durable `analytics_user_first_seen` table (survives chat
    retention)."""
    rows = fetch_user_adoption(
        start=start or (datetime.datetime.utcnow() - datetime.timedelta(days=90)),
        end=end or datetime.datetime.utcnow(),
        db_session=db_session,
    )
    return [
        UserAdoptionResponse(
            new_users=new_users, cumulative_users=cumulative_users, date=date
        )
        for date, new_users, cumulative_users in rows
    ]


class PerUserChatStatsResponse(BaseModel):
    user_id: str
    email: str
    total_messages: int
    total_likes: int
    total_dislikes: int
    last_active: datetime.date


@router.get("/admin/per-user")
def get_per_user_analytics(
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    limit: int = 100,
    _: db_models.User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> list[PerUserChatStatsResponse]:
    """Top users by message volume over the range, from the durable
    analytics_user_daily_stats aggregate — spans full history (survives
    chat retention)."""
    rows = fetch_per_user_chat_stats(
        start=start or (datetime.datetime.utcnow() - datetime.timedelta(days=90)),
        end=end or datetime.datetime.utcnow(),
        db_session=db_session,
        limit=limit,
    )
    return [
        PerUserChatStatsResponse(
            user_id=str(user_id),
            email=email,
            total_messages=int(total_messages),
            total_likes=int(total_likes),
            total_dislikes=int(total_dislikes),
            last_active=last_active,
        )
        for user_id, email, total_messages, total_likes, total_dislikes, last_active in rows
    ]


class PersonaUsageResponse(BaseModel):
    persona_id: int
    name: str
    sessions: int
    messages: int
    likes: int
    dislikes: int
    last_active: datetime.date


@router.get("/admin/persona-usage")
def get_persona_usage_analytics(
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    limit: int = 100,
    _: db_models.User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> list[PersonaUsageResponse]:
    """Most-used assistants over the range, from the durable
    analytics_persona_daily_stats aggregate (spans full history)."""
    rows = fetch_persona_usage(
        start=start or (datetime.datetime.utcnow() - datetime.timedelta(days=90)),
        end=end or datetime.datetime.utcnow(),
        db_session=db_session,
        limit=limit,
    )
    return [
        PersonaUsageResponse(
            persona_id=persona_id,
            name=name,
            sessions=int(sessions),
            messages=int(messages),
            likes=int(likes),
            dislikes=int(dislikes),
            last_active=last_active,
        )
        for persona_id, name, sessions, messages, likes, dislikes, last_active in rows
    ]


class DocumentSetUsageResponse(BaseModel):
    document_set_id: int
    name: str
    # APPROXIMATE: assistant message volume attributed to every document set
    # attached to the assistant (see fetch_document_set_usage). Not a
    # per-query retrieval count.
    attributed_messages: int


@router.get("/admin/document-set-usage")
def get_document_set_usage_analytics(
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    limit: int = 100,
    _: db_models.User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> list[DocumentSetUsageResponse]:
    """Approximate datasets-in-use over the range, derived from assistant
    usage × current persona→document-set attachments (see the db function's
    caveats)."""
    rows = fetch_document_set_usage(
        start=start or (datetime.datetime.utcnow() - datetime.timedelta(days=90)),
        end=end or datetime.datetime.utcnow(),
        db_session=db_session,
        limit=limit,
    )
    return [
        DocumentSetUsageResponse(
            document_set_id=document_set_id,
            name=name,
            attributed_messages=int(attributed_messages),
        )
        for document_set_id, name, attributed_messages in rows
    ]


class DanswerbotAnalyticsResponse(BaseModel):
    total_queries: int
    auto_resolved: int
    date: datetime.date


@router.get("/admin/danswerbot")
def get_danswerbot_analytics(
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    _: db_models.User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> list[DanswerbotAnalyticsResponse]:
    """Slackbot daily stats from `analytics_daily_rollup`. The rollup
    already stores `slackbot_auto_resolved` (clamped to ≥0 at write
    time) so we pass it straight through."""
    rows = fetch_danswerbot_analytics_from_rollup(
        start=start or (datetime.datetime.utcnow() - datetime.timedelta(days=30)),
        end=end or datetime.datetime.utcnow(),
        db_session=db_session,
    )
    return [
        DanswerbotAnalyticsResponse(
            total_queries=int(total_queries),
            auto_resolved=int(auto_resolved),
            date=date,
        )
        for total_queries, auto_resolved, date in rows
    ]


# ---------------------------------------------------------------------------
# Snapshot endpoints (no date range — current state)
# ---------------------------------------------------------------------------


class TotalDocsResponse(BaseModel):
    total_docs_indexed: int
    unique_docs: int


@router.get("/admin/total-docs")
def get_total_docs(
    _: db_models.User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> TotalDocsResponse:
    total, unique = fetch_total_docs_indexed(db_session)
    return TotalDocsResponse(total_docs_indexed=total, unique_docs=unique)


class DocsPerSourceRow(BaseModel):
    source: str
    docs_indexed: int


@router.get("/admin/docs-per-source")
def get_docs_per_source(
    _: db_models.User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> list[DocsPerSourceRow]:
    return [
        DocsPerSourceRow(source=src, docs_indexed=n)
        for src, n in fetch_docs_per_source(db_session)
    ]


class SlackChannelsResponse(BaseModel):
    total_configs: int
    enabled_channels: int


@router.get("/admin/slack-channels")
def get_slack_channels(
    _: db_models.User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> SlackChannelsResponse:
    total_configs, enabled_channels = fetch_slack_bot_channel_stats(db_session)
    return SlackChannelsResponse(
        total_configs=total_configs, enabled_channels=enabled_channels
    )
