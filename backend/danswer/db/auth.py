from collections.abc import AsyncGenerator
from collections.abc import Callable
from typing import Any
from typing import Dict

from fastapi import Depends
from fastapi_users.models import UP
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyAccessTokenDatabase
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from danswer.auth.schemas import UserRole
from danswer.configs.app_configs import DEFAULT_ADMIN_EMAILS
from danswer.db.engine import get_async_session
from danswer.db.engine import get_sqlalchemy_async_engine
from danswer.db.models import AccessToken
from danswer.db.models import OAuthAccount
from danswer.db.models import User
from danswer.utils.variable_functionality import (
    fetch_versioned_implementation_with_fallback,
)


def get_default_admin_user_emails() -> list[str]:
    """Returns a list of emails who should default to Admin role.
    Sourced from the DEFAULT_ADMIN_EMAILS env var (comma-separated).
    The EE version may override this via the versioned implementation."""
    get_default_admin_user_emails_fn: Callable[
        [], list[str]
    ] = fetch_versioned_implementation_with_fallback(
        "danswer.auth.users",
        "get_default_admin_user_emails_",
        lambda: DEFAULT_ADMIN_EMAILS,
    )
    return get_default_admin_user_emails_fn()


async def get_user_count() -> int:
    async with AsyncSession(get_sqlalchemy_async_engine()) as asession:
        stmt = select(func.count(User.id))
        result = await asession.execute(stmt)
        user_count = result.scalar()
        if user_count is None:
            raise RuntimeError("Was not able to fetch the user count.")
        return user_count


# Need to override this because FastAPI Users doesn't give flexibility for backend field creation logic in OAuth flow
class SQLAlchemyUserAdminDB(SQLAlchemyUserDatabase):
    async def create(self, create_dict: Dict[str, Any]) -> UP:
        admin_emails = get_default_admin_user_emails()
        if admin_emails:
            # Explicit allowlist wins — first user is NOT auto-admin.
            is_admin = create_dict["email"] in admin_emails
        else:
            # No allowlist configured: bootstrap convenience — first user is admin.
            user_count = await get_user_count()
            is_admin = user_count == 0
        create_dict["role"] = UserRole.ADMIN if is_admin else UserRole.BASIC
        return await super().create(create_dict)


async def get_user_db(
    session: AsyncSession = Depends(get_async_session),
) -> AsyncGenerator[SQLAlchemyUserAdminDB, None]:
    yield SQLAlchemyUserAdminDB(session, User, OAuthAccount)  # type: ignore


async def get_access_token_db(
    session: AsyncSession = Depends(get_async_session),
) -> AsyncGenerator[SQLAlchemyAccessTokenDatabase, None]:
    yield SQLAlchemyAccessTokenDatabase(session, AccessToken)  # type: ignore
