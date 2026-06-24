"""Sync Highspot Spots -> per-Spot connectors.

Idempotent: ensures exactly one connector + cc-pair (named after the Spot)
exists for every Spot the given Highspot credential can see. Re-running picks up
newly added Spots and is a no-op for already-covered ones, so it's safe to call
repeatedly (e.g. from the admin endpoint in server/documents/connector.py).
"""
import re

from sqlalchemy.orm import Session

from danswer.configs.constants import DocumentSource
from danswer.connectors.highspot.client import HighspotClient
from danswer.connectors.models import InputType
from danswer.db.connector import connector_by_name_source_exists
from danswer.db.connector import create_connector
from danswer.db.connector_credential_pair import add_credential_to_connector
from danswer.db.models import Connector
from danswer.db.models import Credential
from danswer.server.documents.models import ConnectorBase
from danswer.utils.logger import setup_logger

logger = setup_logger()

# Monthly refresh; daily prune — mirrors the existing per-Spot Highspot connector.
HIGHSPOT_MONTHLY_REFRESH_FREQ = 2_592_000
HIGHSPOT_DEFAULT_PRUNE_FREQ = 86_400


def clean_spot_name(title: str) -> str:
    """Tidy a raw Highspot Spot title for use as a connector / cc-pair display
    name: drop control + zero-width characters and collapse internal whitespace.

    NOTE: only the *display* name is cleaned. The original, unmodified title is
    what goes into `spot_names`, because the connector matches Spots by their
    real title (see connector.py::_fetch_spots_to_process) — cleaning that would
    break the match.
    """
    cleaned = re.sub(r"[\x00-\x1f\x7f​-‏﻿]", "", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def sync_highspot_spots_to_connectors(
    credential_id: int,
    db_session: Session,
    refresh_freq: int = HIGHSPOT_MONTHLY_REFRESH_FREQ,
    prune_freq: int = HIGHSPOT_DEFAULT_PRUNE_FREQ,
) -> list[int]:
    """Ensure a per-Spot connector exists for every Spot the credential can see.

    Returns the ids of newly created connectors. Spots already covered by an
    existing Highspot connector's `spot_names` are skipped (idempotent).
    """
    credential = db_session.get(Credential, credential_id)
    if credential is None or not isinstance(credential.credential_json, dict):
        raise ValueError(f"Highspot credential {credential_id} not found")
    cj = credential.credential_json
    if not cj.get("highspot_key") or not cj.get("highspot_secret"):
        raise ValueError(f"Credential {credential_id} is not a Highspot credential")

    client = HighspotClient(
        cj["highspot_key"],
        cj["highspot_secret"],
        base_url=(cj.get("highspot_url") or HighspotClient.BASE_URL),
    )
    spots = [s for s in client.get_spots() if s.get("title")]

    # Spots already covered by any existing Highspot connector (case-insensitive
    # on the ORIGINAL title, which is what's stored in spot_names).
    covered: set[str] = set()
    for connector in db_session.query(Connector).all():
        source = getattr(connector.source, "value", str(connector.source))
        if source.lower() != DocumentSource.HIGHSPOT.value:
            continue
        for name in (connector.connector_specific_config or {}).get("spot_names", []) or []:
            covered.add(name.lower())

    created: list[int] = []
    used_display_names: set[str] = set()
    for spot in spots:
        title = spot["title"]
        if title.lower() in covered:
            continue

        # Skip empty Spots — a connector for a Spot with no items would just run
        # on schedule and index nothing. (Cheap check: counts_total via limit=1.)
        if client.get_spot_items(spot["id"], offset=0, page_size=1).get("counts_total", 0) == 0:
            logger.info(f"Skipping empty Highspot spot '{title}' (no items)")
            continue

        display_name = clean_spot_name(title) or title
        # Connector name must be unique per source; skip on any collision rather
        # than letting create_connector raise mid-loop.
        if display_name.lower() in used_display_names or connector_by_name_source_exists(
            display_name, DocumentSource.HIGHSPOT, db_session
        ):
            logger.warning(
                f"Skipping Highspot spot '{title}': connector name "
                f"'{display_name}' already exists"
            )
            continue

        connector = create_connector(
            ConnectorBase(
                name=display_name,
                source=DocumentSource.HIGHSPOT,
                input_type=InputType.POLL,
                connector_specific_config={"spot_names": [title]},
                refresh_freq=refresh_freq,
                prune_freq=prune_freq,
                disabled=False,
            ),
            db_session,
        )
        add_credential_to_connector(
            connector_id=connector.id,
            credential_id=credential_id,
            cc_pair_name=display_name,
            is_public=True,
            user=None,
            db_session=db_session,
        )
        created.append(connector.id)
        covered.add(title.lower())
        used_display_names.add(display_name.lower())
        logger.info(
            f"Created Highspot connector {connector.id} for spot '{title}' "
            f"(name='{display_name}')"
        )

    logger.info(f"Highspot spot sync complete: {len(created)} new connector(s)")
    return created
