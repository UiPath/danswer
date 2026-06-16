import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from danswer.utils.logger import setup_logger

logger = setup_logger()


def user_facing_http_exception(
    exc: Exception,
    action: str,
    *,
    status_code: int = 400,
    integrity_detail: str | None = None,
) -> HTTPException:
    """Convert an exception into an HTTPException safe to show the user.

    Endpoints historically did ``raise HTTPException(detail=str(e))``, which
    leaks raw psycopg2 / SQLAlchemy text (full SQL statements, parameters,
    constraint names) to the browser whenever a broad ``except Exception``
    catches a database error. This centralizes the translation:

    - ``ValueError`` is treated as an intentional, already-friendly domain
      error and its message is surfaced verbatim.
    - ``IntegrityError`` (unique/foreign-key violations, etc.) gets a generic
      "conflicts with existing data" message, or a caller-supplied
      ``integrity_detail`` for a more specific hint.
    - Anything else is logged server-side and replaced with a generic message.

    ``action`` is a short verb phrase, e.g. "update the document set".
    """
    if isinstance(exc, ValueError):
        return HTTPException(status_code=status_code, detail=str(exc))

    logger.exception(f"Failed to {action}")
    if isinstance(exc, IntegrityError):
        return HTTPException(
            status_code=status_code,
            detail=(
                integrity_detail
                or f"Could not {action} because it conflicts with existing "
                "data (a duplicate entry, or something still referencing it). "
                "Adjust the conflicting item and try again, or contact an "
                "administrator if the problem persists."
            ),
        )
    return HTTPException(
        status_code=status_code,
        detail=(
            f"Something went wrong while trying to {action}. Please try again, "
            "or contact an administrator if the problem persists."
        ),
    )


def get_json_line(json_dict: dict) -> str:
    return json.dumps(json_dict) + "\n"


def mask_string(sensitive_str: str) -> str:
    return "****...**" + sensitive_str[-4:]


def mask_credential_dict(credential_dict: dict[str, Any]) -> dict[str, str]:
    masked_creds = {}
    for key, val in credential_dict.items():
        if not isinstance(val, str):
            raise ValueError(
                f"Unable to mask credentials of type other than string, cannot process request."
                f"Recieved type: {type(val)}"
            )

        masked_creds[key] = mask_string(val)
    return masked_creds
