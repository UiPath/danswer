from cachetools import TTLCache
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from danswer.db.engine import get_session
from danswer.db.models import ApiKey
from danswer.utils.logger import setup_logger


logger = setup_logger()

_API_KEY_HEADER = "X-API-Key"
# Cache API keys for 24 hours (86400 seconds)
cache = TTLCache(maxsize=1000, ttl=86400)  # 24 * 60 * 60 seconds


def validate_api_key(request: Request, db_session: Session = Depends(get_session)):
    if _API_KEY_HEADER not in request.headers:
        return None

    api_key_value = request.headers.get(_API_KEY_HEADER)
    if not api_key_value:
        raise HTTPException(status_code=401, detail="Missing API key")

    # Check if the API key is in cache
    # This is a performance optimization to avoid database lookups
    if api_key_value in cache:
        return None

    api_key = db_session.scalar(
        select(ApiKey).where(ApiKey.hashed_api_key == api_key_value)
    )
    if not api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # If we reach here, the API key is valid
    # Cache it for future requests
    cache[api_key_value] = True
    return None


def request_has_valid_api_key(request: Request, db_session: Session) -> bool:
    """Return True if the request carries a valid X-API-Key.

    These keys are service credentials for automation (they intentionally do NOT
    map to a browser `User`). `current_user` uses this to authorize an api-key
    request as an anonymous service caller instead of 403'ing it into the SSO
    flow once AUTH_TYPE enforces auth (e.g. OIDC). Mirrors `validate_api_key`'s
    lookup + cache exactly, so the two stay consistent.

    NOTE: `db_session` is passed in (not a Depends) because the caller already
    holds a session.
    """
    if _API_KEY_HEADER not in request.headers:
        return False

    api_key_value = request.headers.get(_API_KEY_HEADER)
    if not api_key_value:
        return False

    if api_key_value in cache:
        return True

    api_key = db_session.scalar(
        select(ApiKey).where(ApiKey.hashed_api_key == api_key_value)
    )
    if api_key is None:
        return False

    cache[api_key_value] = True
    return True
