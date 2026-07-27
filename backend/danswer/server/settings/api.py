from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException

from danswer.auth.api_key import validate_api_key
from danswer.auth.users import current_admin_user
from danswer.auth.users import current_user
from danswer.db.models import User
from danswer.server.settings.models import Settings
from danswer.server.settings.store import load_settings
from danswer.server.settings.store import store_settings


admin_router = APIRouter(
    prefix="/admin/settings", dependencies=[Depends(validate_api_key)]
)
basic_router = APIRouter(prefix="/settings", dependencies=[Depends(validate_api_key)])


@admin_router.put("")
def put_settings(
    settings: Settings, _: User | None = Depends(current_admin_user)
) -> None:
    try:
        settings.check_validity()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    store_settings(settings)


@admin_router.patch("")
def patch_settings(
    updates: dict[str, Any], _: User | None = Depends(current_admin_user)
) -> None:
    """Field-level update: merge ONLY the provided keys over the stored settings.

    The whole-object PUT above replaces every field, so a client saving one
    control with a stale snapshot silently clobbers other fields (this wiped the
    assistant-router rulebook twice — a server-set value the UI didn't know
    about). PATCH sends only what changed, so writes can't step on each other."""
    if not updates:
        return
    unknown = set(updates) - set(Settings.__fields__)
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"Unknown settings field(s): {sorted(unknown)}"
        )
    merged = Settings(**{**load_settings().dict(), **updates})
    try:
        merged.check_validity()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    store_settings(merged)


@basic_router.get("")
def fetch_settings(_: User | None = Depends(current_user)) -> Settings:
    return load_settings()
