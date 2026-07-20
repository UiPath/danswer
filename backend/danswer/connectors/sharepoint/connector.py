import html
import io
import os
import random
import re
import time
from collections.abc import Generator
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any
from typing import Optional

import msal  # type: ignore
import requests
from office365.graph_client import GraphClient  # type: ignore
from office365.onedrive.driveitems.driveItem import DriveItem  # type: ignore
from office365.onedrive.sites.site import Site  # type: ignore
from office365.runtime.client_request_exception import (  # type: ignore
    ClientRequestException,
)

from danswer.configs.app_configs import INDEX_BATCH_SIZE
from danswer.configs.constants import DocumentSource
from danswer.connectors.interfaces import GenerateDocumentsOutput
from danswer.connectors.interfaces import LoadConnector
from danswer.connectors.interfaces import PollConnector
from danswer.connectors.interfaces import SecondsSinceUnixEpoch
from danswer.connectors.models import BasicExpertInfo
from danswer.connectors.models import ConnectorMissingCredentialError
from danswer.connectors.models import Document
from danswer.connectors.models import Section
from danswer.file_processing.extract_file_text import extract_file_text
from danswer.utils.logger import setup_logger


logger = setup_logger()

_GRAPH_HOST = "https://graph.microsoft.com"
_GRAPH_BASE = f"{_GRAPH_HOST}/v1.0"
_GRAPH_SCOPE = f"{_GRAPH_HOST}/.default"

# Scrape scope (per-connector config, see SharepointConnector.__init__).
SCOPE_DOCUMENTS = "documents"  # default library files only (original behaviour)
SCOPE_FULL = "full"  # all document libraries + all modern site pages
_VALID_SCOPES = frozenset({SCOPE_DOCUMENTS, SCOPE_FULL})

# --- reliability knobs (learned from upstream Onyx) -------------------------
# Graph throttles aggressively (429) and its gateway returns transient 5xx; a
# single un-retried failure otherwise aborts the whole index. Retry those +
# transport-level drops with Retry-After-aware, jittered backoff.
_GRAPH_MAX_RETRIES = 5
_GRAPH_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_GRAPH_TIMEOUT_SECONDS = 60
_TRANSIENT_TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)
# office365 execute_query() retries on these (429 rate-limit, 503 transient).
_QUERY_RETRYABLE_STATUSES = frozenset({429, 503})


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP-date. Return seconds."""
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _backoff_seconds(attempt: int, retry_after: str | None) -> float:
    """Honor a server Retry-After when present, else capped exponential backoff
    (5s, 10s, 20s, cap 30s) with equal jitter so many items throttled in the
    same window don't all retry on the same tick (thundering herd)."""
    parsed = _parse_retry_after(retry_after)
    if parsed is not None:
        return min(parsed, 60)
    base = min(30, (2**attempt) * 5)
    return base / 2 + random.uniform(0, base / 2)


def _graph_error_code(response: requests.Response | None) -> str:
    if response is None:
        return "<no-response>"
    try:
        return (response.json().get("error", {}) or {}).get("code") or "<no-code>"
    except (ValueError, AttributeError):
        return "<no-code>"


def _is_invalid_request(response: requests.Response | None) -> bool:
    """A corrupt page canvas makes the LIST $expand=canvasLayout 400 with
    'invalidRequest' — the signal to fall back to per-page expansion."""
    return _graph_error_code(response) == "invalidRequest"


def sleep_and_retry(query_obj: Any, method_name: str, max_retries: int = 3) -> Any:
    """Run an office365 client query with retry on rate-limit / transient
    transport failures. Without this a single 429 during bulk indexing aborts
    the whole run."""
    for attempt in range(max_retries + 1):
        try:
            return query_obj.execute_query()
        except _TRANSIENT_TRANSPORT_EXCEPTIONS as e:
            if attempt >= max_retries:
                raise
            wait = _backoff_seconds(attempt, None)
            logger.warning(
                "Transport error on %s (attempt %s/%s): %s. Retrying in %.1fs.",
                method_name,
                attempt + 1,
                max_retries + 1,
                type(e).__name__,
                wait,
            )
            time.sleep(wait)
        except ClientRequestException as e:
            status = e.response.status_code if e.response is not None else None
            wrapped_transport = e.response is None and isinstance(
                e.__cause__ or e.__context__, _TRANSIENT_TRANSPORT_EXCEPTIONS
            )
            retryable = status in _QUERY_RETRYABLE_STATUSES or wrapped_transport
            if not (retryable and attempt < max_retries):
                raise
            retry_after = (
                e.response.headers.get("Retry-After")
                if e.response is not None
                else None
            )
            wait = _backoff_seconds(attempt, retry_after)
            logger.warning(
                "Retryable error on %s (attempt %s/%s): status=%s. Retrying in %.1fs.",
                method_name,
                attempt + 1,
                max_retries + 1,
                status,
                wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"{method_name} failed after {max_retries + 1} attempts")


@dataclass
class SiteData:
    url: str | None
    folder: Optional[str]
    sites: list = field(default_factory=list)
    driveitems: list = field(default_factory=list)


def _convert_driveitem_to_document(
    driveitem: DriveItem,
) -> Document:
    file_text = extract_file_text(
        file_name=driveitem.name,
        file=io.BytesIO(
            sleep_and_retry(driveitem.get_content(), "driveitem.get_content").value
        ),
        break_on_unprocessable=False,
    )

    doc = Document(
        id=driveitem.id,
        sections=[Section(link=driveitem.web_url, text=file_text)],
        source=DocumentSource.SHAREPOINT,
        semantic_identifier=driveitem.name,
        doc_updated_at=driveitem.last_modified_datetime.replace(tzinfo=timezone.utc),
        primary_owners=[
            BasicExpertInfo(
                display_name=driveitem.last_modified_by.user.displayName,
                email=driveitem.last_modified_by.user.email,
            )
        ],
        metadata={},
    )
    return doc


# --- site pages (.aspx modern pages) ---------------------------------------


def _html_to_text(inner_html: str) -> str:
    """Turn a text-webpart's innerHtml into readable text, preserving basic
    structure (line breaks, list bullets, headings)."""
    text = re.sub(r"<br\s*/?>", "\n", inner_html)
    text = re.sub(r"<li>", "• ", text)
    text = re.sub(r"</li>", "\n", text)
    text = re.sub(r"<h[1-6][^>]*>", "\n## ", text)
    text = re.sub(r"</h[1-6]>", "\n", text)
    text = re.sub(r"<p[^>]*>", "\n", text)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n\s*\n", "\n\n", text).strip()


def _extract_sitepage_text(site_page: dict[str, Any]) -> str:
    """Extract readable text from a SharePoint modern page's canvas layout —
    text webparts (innerHtml) and standard webparts (serverProcessedContent
    searchable plain texts / title / description)."""
    parts: list[str] = []
    title = site_page.get("title") or ""
    description = site_page.get("description") or ""
    if title:
        parts.append(f"# {title}")
    if description:
        parts.append(description)

    canvas = site_page.get("canvasLayout") or {}
    for section in canvas.get("horizontalSections", []) or []:
        for column in section.get("columns", []) or []:
            for webpart in column.get("webparts", []) or []:
                wp_type = webpart.get("@odata.type", "")
                if wp_type == "#microsoft.graph.textWebPart":
                    text = _html_to_text(webpart.get("innerHtml", "") or "")
                    if text:
                        parts.append(text)
                elif wp_type == "#microsoft.graph.standardWebPart":
                    data = webpart.get("data", {}) or {}
                    server = data.get("serverProcessedContent", {}) or {}
                    for item in server.get("searchablePlainTexts", []) or []:
                        if isinstance(item, dict) and item.get("value"):
                            key = item.get("key", "")
                            value = item["value"]
                            parts.append(f"## {value}" if key == "title" else value)
                    if data.get("description"):
                        parts.append(data["description"])
                    wp_title = data.get("title", "")
                    if wp_title and wp_title != data.get("description"):
                        parts.append(f"## {wp_title}")

    text = "\n\n".join(p for p in parts if p).strip()
    return text or title


def _parse_graph_datetime(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _site_page_in_time_window(
    page: dict[str, Any], start: datetime | None, end: datetime | None
) -> bool:
    if start is None and end is None:
        return True
    dt = _parse_graph_datetime(page.get("lastModifiedDateTime"))
    if dt is None:
        return True
    return (start is None or dt >= start) and (end is None or dt <= end)


def _convert_sitepage_to_document(
    site_page: dict[str, Any], site_name: str | None
) -> Document:
    web_url = site_page.get("webUrl", "")
    owners = []
    created_by = (site_page.get("createdBy") or {}).get("user") or {}
    if created_by.get("displayName"):
        owners.append(
            BasicExpertInfo(
                display_name=created_by.get("displayName"),
                email=created_by.get("email"),
            )
        )
    return Document(
        id=f"sharepoint_page__{site_page.get('id') or web_url}",
        sections=[Section(link=web_url, text=_extract_sitepage_text(site_page))],
        source=DocumentSource.SHAREPOINT,
        semantic_identifier=site_page.get("title") or site_page.get("name") or web_url,
        doc_updated_at=_parse_graph_datetime(site_page.get("lastModifiedDateTime")),
        primary_owners=owners or None,
        metadata={"type": "site_page", "site": site_name}
        if site_name
        else {"type": "site_page"},
    )


class SharepointConnector(LoadConnector, PollConnector):
    def __init__(
        self,
        batch_size: int = INDEX_BATCH_SIZE,
        sites: list[str] = [],
        scrape_scope: str = SCOPE_DOCUMENTS,
    ) -> None:
        self.batch_size = batch_size
        self.graph_client: GraphClient | None = None
        self.msal_app: msal.ConfidentialClientApplication | None = None
        # How much of each site to index:
        #   "documents" (default) — files from the default document library only,
        #                           the original behaviour (PDF/Word/PPT/… text).
        #   "full"                — recurse the WHOLE site: every document library
        #                           plus all modern site pages (.aspx canvas).
        scope = (scrape_scope or SCOPE_DOCUMENTS).strip().lower()
        self.scrape_scope = scope if scope in _VALID_SCOPES else SCOPE_DOCUMENTS
        self.site_data: list[SiteData] = self._extract_site_and_folder(sites)

    @staticmethod
    def _extract_site_and_folder(site_urls: list[str]) -> list[SiteData]:
        site_data_list = []
        for url in site_urls:
            parts = url.strip().split("/")
            if "sites" in parts:
                sites_index = parts.index("sites")
                site_url = "/".join(parts[: sites_index + 2])
                folder = (
                    parts[sites_index + 2] if len(parts) > sites_index + 2 else None
                )
                site_data_list.append(
                    SiteData(url=site_url, folder=folder, sites=[], driveitems=[])
                )
        return site_data_list

    # --- raw Graph GET with retry (for the /pages API) ---------------------

    def _get_access_token(self) -> str:
        if self.msal_app is None:
            raise ConnectorMissingCredentialError("Sharepoint")
        token = self.msal_app.acquire_token_for_client(scopes=[_GRAPH_SCOPE])
        access_token = token.get("access_token")
        if not access_token:
            raise RuntimeError(f"Failed to acquire Graph token: {token.get('error')}")
        return access_token

    def _graph_get(
        self, url: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Authenticated Graph GET with Retry-After-aware retry on 429/5xx +
        transport errors, re-acquiring the token in case it expired mid-run."""
        headers = {"Authorization": f"Bearer {self._get_access_token()}"}
        for attempt in range(_GRAPH_MAX_RETRIES + 1):
            try:
                resp = requests.get(
                    url, headers=headers, params=params, timeout=_GRAPH_TIMEOUT_SECONDS
                )
                if (
                    resp.status_code in _GRAPH_RETRYABLE_STATUSES
                    and attempt < _GRAPH_MAX_RETRIES
                ):
                    wait = _backoff_seconds(attempt, resp.headers.get("Retry-After"))
                    logger.warning(
                        "Graph %s on attempt %s, retrying in %.1fs: %s",
                        resp.status_code,
                        attempt + 1,
                        wait,
                        url,
                    )
                    time.sleep(wait)
                    headers = {"Authorization": f"Bearer {self._get_access_token()}"}
                    continue
                resp.raise_for_status()
                return resp.json()
            except _TRANSIENT_TRANSPORT_EXCEPTIONS:
                if attempt >= _GRAPH_MAX_RETRIES:
                    raise
                time.sleep(min(2**attempt, 60))
        raise RuntimeError(f"Graph GET failed after retries: {url}")

    def _fetch_site_pages(
        self,
        site_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Yield modern site pages with canvas content, paginating nextLink and
        filtering by the time window. Falls back to per-page expansion if the
        list-level $expand=canvasLayout 400s (one corrupt page poisons the
        whole list response)."""
        base = f"{_GRAPH_BASE}/sites/{site_id}/pages/microsoft.graph.sitePage"
        url: str | None = base
        params: dict[str, str] | None = {"$expand": "canvasLayout"}
        seen: set[str] = set()
        while url:
            try:
                data = self._graph_get(url, params)
            except requests.HTTPError as e:
                r = e.response
                if r is not None and r.status_code == 404:
                    logger.warning("Site pages not found for site %s", site_id)
                    return
                if r is not None and r.status_code == 400 and _is_invalid_request(r):
                    logger.warning(
                        "list $expand=canvasLayout 400 for site %s — per-page fallback",
                        site_id,
                    )
                    yield from self._fetch_site_pages_individually(
                        base, start, end, seen
                    )
                    return
                raise
            params = None  # nextLink already carries the query
            for page in data.get("value", []):
                if not _site_page_in_time_window(page, start, end):
                    continue
                pid = page.get("id")
                if pid:
                    seen.add(pid)
                yield page
            url = data.get("@odata.nextLink")

    def _fetch_site_pages_individually(
        self,
        base: str,
        start: datetime | None,
        end: datetime | None,
        skip_ids: set[str],
    ) -> Generator[dict[str, Any], None, None]:
        """List pages without $expand, then expand each individually so one
        corrupt page only loses its own canvas instead of the whole site."""
        url: str | None = base
        while url:
            try:
                data = self._graph_get(url)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    return
                raise
            for page in data.get("value", []):
                if not _site_page_in_time_window(page, start, end):
                    continue
                pid = page.get("id")
                if pid and pid in skip_ids:
                    continue
                yield self._try_expand_single_page(base, pid, page) if pid else page
            url = data.get("@odata.nextLink")

    def _try_expand_single_page(
        self, base: str, page_id: str, fallback: dict[str, Any]
    ) -> dict[str, Any]:
        collection = base.removesuffix("/microsoft.graph.sitePage")
        url = f"{collection}/{page_id}/microsoft.graph.sitePage"
        try:
            return self._graph_get(url, {"$expand": "canvasLayout"})
        except requests.HTTPError as e:
            if (
                e.response is not None
                and e.response.status_code == 400
                and _is_invalid_request(e.response)
            ):
                logger.warning(
                    "canvas expand failed for page %s — indexing metadata only",
                    fallback.get("name", page_id),
                )
                return fallback
            raise

    def _drive_files(
        self, drive: Any, folder: str | None, filter_str: str
    ) -> list[DriveItem]:
        """Recursively fetch files from one drive (document library), applying
        the optional folder filter. Returns [] for drives with no root (some
        list objects don't expose a real .drive.root)."""
        try:
            query = drive.root.get_files(True, 1000)
            if filter_str:
                query = query.filter(filter_str)
            driveitems = sleep_and_retry(query, "drive.root.get_files")
            if folder:
                return [
                    item for item in driveitems if folder in item.parent_reference.path
                ]
            return list(driveitems)
        except Exception:
            # Sites/lists include things that do not contain .drive.root so this
            # fails — fine, there are no documents in those.
            return []

    def _populate_sitedata_driveitems(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> None:
        filter_str = ""
        if start is not None and end is not None:
            filter_str = f"last_modified_datetime ge {start.isoformat()} and last_modified_datetime le {end.isoformat()}"

        for element in self.site_data:
            if self.scrape_scope == SCOPE_FULL:
                # Every document library on the site, not just the default drive.
                for site in element.sites:
                    try:
                        drives = sleep_and_retry(site.drives.get(), "site.drives.get")
                    except Exception:
                        logger.exception("Failed to list drives for a site")
                        drives = []
                    for drive in drives:
                        element.driveitems.extend(
                            self._drive_files(drive, element.folder, filter_str)
                        )
                continue

            # documents mode: original behaviour (default document library).
            sites: list[Site] = []
            for site in element.sites:
                site_sublist = sleep_and_retry(site.lists.get(), "site.lists.get")
                sites.extend(site_sublist)

            for site in sites:
                element.driveitems.extend(
                    self._drive_files(site.drive, element.folder, filter_str)
                )

    def _populate_sitedata_sites(self) -> None:
        if self.graph_client is None:
            raise ConnectorMissingCredentialError("Sharepoint")

        if self.site_data:
            for element in self.site_data:
                element.sites = [
                    sleep_and_retry(
                        self.graph_client.sites.get_by_url(element.url).get(),
                        "sites.get_by_url",
                    )
                ]
        else:
            sites = sleep_and_retry(self.graph_client.sites.get(), "sites.get")
            self.site_data = [
                SiteData(url=None, folder=None, sites=sites, driveitems=[])
            ]

    def _fetch_from_sharepoint(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> GenerateDocumentsOutput:
        if self.graph_client is None:
            raise ConnectorMissingCredentialError("Sharepoint")

        self._populate_sitedata_sites()
        self._populate_sitedata_driveitems(start=start, end=end)

        # goes over all urls, converts them into Document objects and then yields them in batches
        doc_batch: list[Document] = []
        for element in self.site_data:
            for driveitem in element.driveitems:
                logger.debug(f"Processing: {driveitem.web_url}")
                doc_batch.append(_convert_driveitem_to_document(driveitem))

                if len(doc_batch) >= self.batch_size:
                    yield doc_batch
                    doc_batch = []

            # Modern site pages (.aspx) — canvas content the drive never exposes.
            if self.scrape_scope == SCOPE_FULL:
                for site in element.sites:
                    site_id = getattr(site, "id", None)
                    if not site_id:
                        continue
                    site_name = getattr(site, "display_name", None) or getattr(
                        site, "name", None
                    )
                    try:
                        for page in self._fetch_site_pages(site_id, start, end):
                            doc_batch.append(
                                _convert_sitepage_to_document(page, site_name)
                            )
                            if len(doc_batch) >= self.batch_size:
                                yield doc_batch
                                doc_batch = []
                    except Exception:
                        # A site with no pages library / no access shouldn't abort
                        # the rest of the run.
                        logger.exception(
                            "Failed to fetch site pages for site %s", site_id
                        )
        yield doc_batch

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        sp_client_id = credentials["sp_client_id"]
        sp_client_secret = credentials["sp_client_secret"]
        sp_directory_id = credentials["sp_directory_id"]

        authority_url = f"https://login.microsoftonline.com/{sp_directory_id}"
        self.msal_app = msal.ConfidentialClientApplication(
            authority=authority_url,
            client_id=sp_client_id,
            client_credential=sp_client_secret,
        )

        def _acquire_token_func() -> dict[str, Any]:
            """Acquire token via the shared MSAL app (token-cached)."""
            if self.msal_app is None:
                raise ConnectorMissingCredentialError("Sharepoint")
            return self.msal_app.acquire_token_for_client(scopes=[_GRAPH_SCOPE])

        self.graph_client = GraphClient(_acquire_token_func)
        return None

    def load_from_state(self) -> GenerateDocumentsOutput:
        return self._fetch_from_sharepoint()

    def poll_source(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch
    ) -> GenerateDocumentsOutput:
        start_datetime = datetime.utcfromtimestamp(start)
        end_datetime = datetime.utcfromtimestamp(end)
        return self._fetch_from_sharepoint(start=start_datetime, end=end_datetime)


if __name__ == "__main__":
    connector = SharepointConnector(sites=os.environ["SITES"].split(","))

    connector.load_credentials(
        {
            "sp_client_id": os.environ["SP_CLIENT_ID"],
            "sp_client_secret": os.environ["SP_CLIENT_SECRET"],
            "sp_directory_id": os.environ["SP_CLIENT_DIRECTORY_ID"],
        }
    )
    document_batches = connector.load_from_state()
    print(next(document_batches))
