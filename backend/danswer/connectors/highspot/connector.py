"""Highspot connector for Darwin.

Indexes Spots and the Items inside them via Highspot's REST API
(https://api-su2.highspot.com/v1.0/). For each Item we build a
`Document` whose section text is one of:

  1. WebLink items → headless-Chromium scrape of the linked URL
     (with a fallback to title+description if the scrape fails).
  2. File items with a downloadable, supported extension
     (.pdf, .docx, .pptx, .xlsx, .eml, .epub, .html, .txt) →
     `extract_file_text` over the bytes returned by the
     `items/{id}/content` endpoint.
  3. Anything else → `title + "\\n" + description` as a default.

Ported from upstream Onyx with these structural adjustments for this
fork:

- Drops the Slim/perm-sync interface (this fork has no
  `SlimConnectorWithPermSync`, `SlimDocument`, or
  `IndexingHeartbeatInterface`).
- Uses `Section` instead of upstream's `TextSection`.
- Replaces `OnyxFileExtensions` with an inline allowlist matching this
  fork's `extract_file_text` capabilities.
- `extract_file_text` argument order is `(file_name, file, ...)` here
  vs upstream's `(file, file_name, ...)`.
- `doc_updated_at` is parsed to `datetime` before assignment because
  this fork types it as `datetime | None`.
"""
import os
from collections.abc import Callable
from datetime import datetime
from io import BytesIO
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from playwright.sync_api import BrowserContext
from playwright.sync_api import sync_playwright
from pydantic import BaseModel

from danswer.configs.app_configs import INDEX_BATCH_SIZE
from danswer.configs.constants import DocumentSource
from danswer.connectors.highspot.client import HighspotAuthenticationError
from danswer.connectors.highspot.client import HighspotClient
from danswer.connectors.highspot.client import HighspotClientError
from danswer.connectors.highspot.utils import scrape_url_content
from danswer.connectors.interfaces import GenerateDocumentsOutput
from danswer.connectors.interfaces import LoadConnector
from danswer.connectors.interfaces import PollConnector
from danswer.connectors.interfaces import SecondsSinceUnixEpoch
from danswer.connectors.models import ConnectorMissingCredentialError
from danswer.connectors.models import Document
from danswer.connectors.models import Section
from danswer.file_processing.extract_file_text import extract_file_text
from danswer.utils.logger import setup_logger


logger = setup_logger()

# Inline allowlist mirroring this fork's extract_file_text dispatch
# (see backend/danswer/file_processing/extract_file_text.py:271). We
# only attempt binary download + extraction for items whose
# `content_name` ends in one of these — anything else falls through to
# title+description.
_SUPPORTED_FILE_EXTENSIONS = frozenset(
    {".pdf", ".docx", ".pptx", ".xlsx", ".eml", ".epub", ".html", ".txt"}
)

# How many docs to buffer before yielding to the downstream indexer.
# Decoupled from `self.batch_size` (which controls Highspot API
# pagination): the server-side page can still be 16 items, but we
# yield in smaller chunks so the indexer's `docs_indexed` counter
# updates more often. Per-item processing in this connector can be
# slow (Playwright scrape, big-PDF extraction), so yielding every
# `INDEX_BATCH_SIZE` items can mean minutes between UI counter
# updates. 4 keeps the per-yield Vespa write overhead small while
# giving admins meaningful progress feedback.
_YIELD_BATCH_SIZE = 4


class HighspotSpot(BaseModel):
    id: str
    name: str


def _parse_doc_updated_at(value: Any) -> datetime | None:
    """Parse Highspot's `date_updated` field.

    Highspot returns ISO-8601 strings ending in 'Z'. This fork's
    `Document.doc_updated_at` is `datetime | None`, so we have to
    parse before assignment. Returns None on any failure rather than
    raising — a missing timestamp is preferable to dropping the doc.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class HighspotConnector(LoadConnector, PollConnector):
    """
    Connector for loading data from Highspot.

    Retrieves content from specified Spots using the Highspot API.
    If `spot_names` is empty/None, retrieves content from every Spot
    accessible to the credential.
    """

    def __init__(
        self,
        spot_names: list[str] | None = None,
        batch_size: int = INDEX_BATCH_SIZE,
    ):
        self.spot_names = spot_names or []
        self.batch_size = batch_size

        self._client: Optional[HighspotClient] = None
        self.highspot_url: Optional[str] = None
        self.key: Optional[str] = None
        self.secret: Optional[str] = None

    @property
    def client(self) -> HighspotClient:
        if self._client is None:
            if not self.key or not self.secret:
                raise ConnectorMissingCredentialError("Highspot")
            base_url = (
                self.highspot_url
                if self.highspot_url is not None
                else HighspotClient.BASE_URL
            )
            self._client = HighspotClient(self.key, self.secret, base_url=base_url)
        return self._client

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        logger.info("Loading Highspot credentials")
        self.highspot_url = credentials.get("highspot_url")
        self.key = credentials.get("highspot_key")
        self.secret = credentials.get("highspot_secret")
        return None

    # ------------------------------------------------------------------
    # Spot discovery
    # ------------------------------------------------------------------
    def _fetch_spots(self) -> list[HighspotSpot]:
        return [
            HighspotSpot(id=spot["id"], name=spot["title"])
            for spot in self.client.get_spots()
        ]

    def _fetch_spots_to_process(self) -> list[HighspotSpot]:
        spots = self._fetch_spots()
        if not spots:
            raise ValueError("No spots found in Highspot.")

        if self.spot_names:
            lower_spot_names = [name.lower() for name in self.spot_names]
            spots_to_process = [
                spot for spot in spots if spot.name.lower() in lower_spot_names
            ]
            if not spots_to_process:
                raise ValueError(
                    f"No valid spots found in Highspot. "
                    f"Found {[s.name for s in spots]} but {self.spot_names} "
                    f"were requested."
                )
            return spots_to_process

        return spots

    # ------------------------------------------------------------------
    # LoadConnector / PollConnector entry points
    # ------------------------------------------------------------------
    def load_from_state(self) -> GenerateDocumentsOutput:
        return self.poll_source(None, None)

    def poll_source(
        self,
        start: SecondsSinceUnixEpoch | None,
        end: SecondsSinceUnixEpoch | None,
    ) -> GenerateDocumentsOutput:
        spots_to_process = self._fetch_spots_to_process()

        # Shared Playwright browser for all WebLink scrapes in this
        # poll. Mirrors the lifecycle in
        # `connectors/web/connector.py`: spawn ONCE, reuse across
        # items via `context.new_page()` / `page.close()`, tear down
        # at end. The spawn-per-item pattern (upstream Onyx) starves
        # the worker's FDs/RAM and causes co-running connectors
        # (e.g. Slack) to fail with `IncompleteRead` mid-response.
        # Lazy-init: only spawned if we hit a WebLink item.
        playwright_inst = None
        browser = None
        scrape_context: BrowserContext | None = None

        def _ensure_browser() -> BrowserContext:
            nonlocal playwright_inst, browser, scrape_context
            if scrape_context is not None:
                return scrape_context
            playwright_inst = sync_playwright().start()
            browser = playwright_inst.chromium.launch(headless=True)
            scrape_context = browser.new_context()
            return scrape_context

        doc_batch: list[Document] = []
        try:
            for spot in spots_to_process:
                try:
                    offset = 0
                    has_more = True

                    while has_more:
                        logger.info(
                            "Retrieving items from spot %s, offset %s",
                            spot.name,
                            offset,
                        )
                        response = self.client.get_spot_items(
                            spot_id=spot.id,
                            offset=offset,
                            page_size=self.batch_size,
                        )
                        items = response.get("collection", [])
                        logger.info(
                            "Received %s items from spot %s", len(items), spot.name
                        )
                        if not items:
                            has_more = False
                            continue

                        for item in items:
                            item_id = None
                            try:
                                item_id = item.get("id")
                                if not item_id:
                                    logger.warning("Item without ID found, skipping")
                                    continue

                                # Time-window filter (poll mode) — applied on the
                                # LIST item's `date_updated` BEFORE the per-item
                                # get_item() call, so incremental polls skip the
                                # detail fetch + content download/scrape for items
                                # unchanged within the window. The list response
                                # already carries `date_updated`, so this is a true
                                # delta fetch rather than enumerating every item.
                                if start or end:
                                    parsed = _parse_doc_updated_at(
                                        item.get("date_updated")
                                    )
                                    if parsed is None:
                                        # No usable timestamp — skip in poll
                                        # mode rather than reindex unconditionally.
                                        continue
                                    ts = parsed.timestamp()
                                    if (start and ts < start) or (end and ts > end):
                                        continue

                                item_details = self.client.get_item(item_id)
                                if not item_details:
                                    logger.warning(
                                        "Item %s details not found, skipping",
                                        item_id,
                                    )
                                    continue

                                content = self._get_item_content(
                                    item_details,
                                    scrape_context_factory=_ensure_browser,
                                )
                                title = item_details.get("title", "")
                                doc_updated_at = _parse_doc_updated_at(
                                    item_details.get("date_updated")
                                )

                                doc_batch.append(
                                    Document(
                                        id=f"HIGHSPOT_{item_id}",
                                        sections=[
                                            Section(
                                                link=item_details.get(
                                                    "url",
                                                    f"https://www.highspot.com/items/{item_id}",
                                                ),
                                                text=content,
                                            )
                                        ],
                                        source=DocumentSource.HIGHSPOT,
                                        semantic_identifier=title,
                                        metadata={
                                            "spot_name": spot.name,
                                            "type": item_details.get(
                                                "content_type", ""
                                            ),
                                            "created_at": item_details.get(
                                                "date_added", ""
                                            ),
                                            "author": item_details.get("author", ""),
                                            "language": item_details.get(
                                                "language", ""
                                            ),
                                            "can_download": str(
                                                item_details.get("can_download", False)
                                            ),
                                        },
                                        doc_updated_at=doc_updated_at,
                                    )
                                )

                                if len(doc_batch) >= _YIELD_BATCH_SIZE:
                                    yield doc_batch
                                    doc_batch = []

                            except HighspotClientError as e:
                                logger.error(
                                    "Error retrieving item %s: %s",
                                    item_id or "(unknown)",
                                    str(e),
                                )
                            except Exception as e:
                                logger.error(
                                    "Unexpected error for item %s: %s",
                                    item_id or "(unknown)",
                                    str(e),
                                )

                        has_more = len(items) >= self.batch_size
                        offset += self.batch_size

                except (HighspotClientError, ValueError) as e:
                    logger.error("Error processing spot %s: %s", spot.name, str(e))
                    raise
                except Exception as e:
                    logger.error(
                        "Unexpected error processing spot %s: %s",
                        spot.name,
                        str(e),
                    )
                    raise

        except Exception as e:
            logger.error("Error in Highspot connector: %s", str(e))
            raise

        finally:
            # Tear down the shared Playwright browser if we ever
            # opened one. Wrapped in try/except so a teardown failure
            # never masks the real exception or interrupts the
            # generator's yield-on-success path.
            if browser is not None:
                try:
                    browser.close()
                except Exception as e:
                    logger.debug("Error closing shared browser: %s", str(e))
            if playwright_inst is not None:
                try:
                    playwright_inst.stop()
                except Exception as e:
                    logger.debug("Error stopping shared playwright: %s", str(e))

        if doc_batch:
            yield doc_batch

    # ------------------------------------------------------------------
    # Item content extraction
    # ------------------------------------------------------------------
    def _get_item_content(
        self,
        item_details: Dict[str, Any],
        scrape_context_factory: Optional[Callable[[], BrowserContext]] = None,
    ) -> str:
        item_id = item_details.get("id", "")
        content_name = item_details.get("content_name", "") or ""
        is_valid_format = bool(content_name) and "." in content_name
        file_extension = (
            "." + content_name.rsplit(".", 1)[-1].lower() if is_valid_format else ""
        )
        can_download = bool(item_details.get("can_download", False))
        content_type = item_details.get("content_type", "")

        title, description = self._extract_title_and_description(item_details)
        default_content = f"{title}\n{description}"
        logger.info(
            "Processing item %s (content_type=%s, ext=%s, content_name=%s)",
            item_id,
            content_type,
            file_extension,
            content_name,
        )

        try:
            if content_type == "WebLink":
                url = item_details.get("url")
                if not url:
                    return default_content
                # Use the shared browser/context owned by the
                # caller; only fall back to a fresh spawn if no
                # factory was provided (e.g. the __main__ smoke
                # test).
                shared_context = (
                    scrape_context_factory() if scrape_context_factory else None
                )
                content = scrape_url_content(
                    url,
                    scroll_before_scraping=True,
                    context=shared_context,
                )
                return content if content else default_content

            elif (
                is_valid_format
                and file_extension in _SUPPORTED_FILE_EXTENSIONS
                and can_download
            ):
                content_response = self.client.get_item_content(item_id)
                if not content_response:
                    return default_content
                # Note: this fork's extract_file_text has the
                # arguments in a different order than upstream.
                text_content = extract_file_text(
                    file_name=content_name,
                    file=BytesIO(content_response),
                    break_on_unprocessable=False,
                )
                return text_content if text_content else default_content

            logger.info(
                "Item %s has no extractable body (ext=%s, can_download=%s); "
                "using title+description.",
                item_id,
                file_extension,
                can_download,
            )
            return default_content

        except HighspotClientError as e:
            logger.warning(
                "Could not retrieve content for item %s: %s",
                item_id or "(unknown)",
                str(e),
            )
            return default_content
        except ValueError as e:
            logger.error("Value error for item %s: %s", item_id or "(unknown)", str(e))
            return default_content
        except Exception as e:
            logger.error(
                "Unexpected error retrieving content for item %s: %s",
                item_id or "(unknown)",
                str(e),
            )
            return default_content

    def _extract_title_and_description(
        self, item_details: Dict[str, Any]
    ) -> tuple[str, str]:
        title = item_details.get("title", "") or ""
        description = item_details.get("description", "") or ""
        return title, description

    # ------------------------------------------------------------------
    # Credential validation (called by admin UI)
    # ------------------------------------------------------------------
    def validate_credentials(self) -> bool:
        try:
            return self.client.health_check()
        except HighspotAuthenticationError:
            return False
        except Exception as e:
            logger.error("Failed to validate Highspot credentials: %s", str(e))
            return False


if __name__ == "__main__":
    spot_names: List[str] = []
    if os.environ.get("HIGHSPOT_SPOT_NAMES"):
        spot_names = [
            s.strip() for s in os.environ["HIGHSPOT_SPOT_NAMES"].split(",") if s.strip()
        ]
    connector = HighspotConnector(spot_names=spot_names)
    connector.load_credentials(
        {
            "highspot_key": os.environ.get("HIGHSPOT_KEY"),
            "highspot_secret": os.environ.get("HIGHSPOT_SECRET"),
            "highspot_url": os.environ.get("HIGHSPOT_URL"),
        }
    )
    for batch in connector.load_from_state():
        for doc in batch:
            print(doc)
        break
