import io
import ipaddress
import random
import re
import socket
import time
from datetime import datetime
from datetime import timezone
from enum import Enum
from typing import Any
from typing import cast
from typing import Tuple
from urllib.parse import urljoin
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from oauthlib.oauth2 import BackendApplicationClient
from playwright.sync_api import BrowserContext
from playwright.sync_api import Playwright
from playwright.sync_api import sync_playwright
from requests_oauthlib import OAuth2Session  # type:ignore

from danswer.configs.app_configs import INDEX_BATCH_SIZE
from danswer.configs.app_configs import WEB_CONNECTOR_MAX_PAGES
from danswer.configs.app_configs import WEB_CONNECTOR_MAX_RETRIES
from danswer.configs.app_configs import WEB_CONNECTOR_OAUTH_CLIENT_ID
from danswer.configs.app_configs import WEB_CONNECTOR_OAUTH_CLIENT_SECRET
from danswer.configs.app_configs import WEB_CONNECTOR_OAUTH_TOKEN_URL
from danswer.configs.app_configs import WEB_CONNECTOR_PAGE_TIMEOUT_MS
from danswer.configs.app_configs import WEB_CONNECTOR_VALIDATE_URLS
from danswer.configs.constants import DocumentSource
from danswer.connectors.interfaces import GenerateDocumentsOutput
from danswer.connectors.interfaces import LoadConnector
from danswer.connectors.interfaces import PollConnector
from danswer.connectors.interfaces import SecondsSinceUnixEpoch
from danswer.connectors.models import Document
from danswer.connectors.models import Section
from danswer.file_processing.extract_file_text import pdf_to_text
from danswer.file_processing.html_utils import web_html_cleanup
from danswer.utils.logger import setup_logger

logger = setup_logger()

# Many docs sites / WAFs (Cloudflare etc.) 403 or rate-limit the default
# headless-Chromium / bare-requests user agent. Present as a normal browser.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _is_browser_dead(exc: Exception) -> bool:
    """Heuristic: did the exception kill the browser/context (vs. just this
    page)? Only then is a full Playwright restart warranted; otherwise we retry
    with a fresh page on the existing browser."""
    msg = str(exc).lower()
    return any(
        marker in msg
        for marker in (
            "browser has been closed",
            "browser closed",
            "crash",
            "target closed",
        )
    )


class WEB_CONNECTOR_VALID_SETTINGS(str, Enum):
    # Given a base site, index everything under that path
    RECURSIVE = "recursive"
    # Given a URL, index only the given page
    SINGLE = "single"
    # Given a sitemap.xml URL, parse all the pages in it
    SITEMAP = "sitemap"
    # Given a file upload where every line is a URL, parse all the URLs provided
    UPLOAD = "upload"


def protected_url_check(url: str) -> None:
    """Couple considerations:
    - DNS mapping changes over time so we don't want to cache the results
    - Fetching this is assumed to be relatively fast compared to other bottlenecks like reading
      the page or embedding the contents
    - To be extra safe, all IPs associated with the URL must be global
    - This is to prevent misuse and not explicit attacks
    """
    if not WEB_CONNECTOR_VALIDATE_URLS:
        return

    parse = urlparse(url)
    if parse.scheme != "http" and parse.scheme != "https":
        raise ValueError("URL must be of scheme https?://")

    if not parse.hostname:
        raise ValueError("URL must include a hostname")

    try:
        # This may give a large list of IP addresses for domains with extensive DNS configurations
        # such as large distributed systems of CDNs
        info = socket.getaddrinfo(parse.hostname, None)
    except socket.gaierror as e:
        raise ConnectionError(f"DNS resolution failed for {parse.hostname}: {e}")

    for address in info:
        ip = address[4][0]
        if not ipaddress.ip_address(ip).is_global:
            raise ValueError(
                f"Non-global IP address detected: {ip}, skipping page {url}. "
                f"The Web Connector is not allowed to read loopback, link-local, or private ranges"
            )


def check_internet_connection(url: str) -> None:
    try:
        response = requests.get(url, timeout=3)
        response.raise_for_status()
    except (requests.RequestException, ValueError):
        raise Exception(f"Unable to reach {url} - check your internet connection")


def is_valid_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except ValueError:
        return False


def get_internal_links(
    base_url: str,
    url: str,
    soup: BeautifulSoup,
    should_ignore_pound: bool = True,
    allowed_prefixes: list[str] | None = None,
) -> set[str]:
    # Recursion scope: a link is followed only if it lives under one of these
    # prefixes. Defaults to [base_url]; for UiPath docs version-expansion this is
    # the set of the latest-N version base URLs so all N subtrees are crawled
    # (and older versions are excluded).
    prefixes = allowed_prefixes if allowed_prefixes else [base_url]
    internal_links = set()
    for link in cast(list[dict[str, Any]], soup.find_all("a")):
        href = cast(str | None, link.get("href"))
        if not href:
            continue

        if should_ignore_pound and "#" in href:
            href = href.split("#")[0]

        if not is_valid_url(href):
            # Relative path handling
            href = urljoin(url, href)

        if urlparse(href).netloc == urlparse(url).netloc and any(
            prefix in href for prefix in prefixes
        ):
            internal_links.add(href)
    return internal_links


def start_playwright() -> Tuple[Playwright, BrowserContext]:
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)

    context = browser.new_context(user_agent=DEFAULT_USER_AGENT)

    if (
        WEB_CONNECTOR_OAUTH_CLIENT_ID
        and WEB_CONNECTOR_OAUTH_CLIENT_SECRET
        and WEB_CONNECTOR_OAUTH_TOKEN_URL
    ):
        client = BackendApplicationClient(client_id=WEB_CONNECTOR_OAUTH_CLIENT_ID)
        oauth = OAuth2Session(client=client)
        token = oauth.fetch_token(
            token_url=WEB_CONNECTOR_OAUTH_TOKEN_URL,
            client_id=WEB_CONNECTOR_OAUTH_CLIENT_ID,
            client_secret=WEB_CONNECTOR_OAUTH_CLIENT_SECRET,
        )
        context.set_extra_http_headers(
            {"Authorization": "Bearer {}".format(token["access_token"])}
        )

    return playwright, context


def extract_urls_from_sitemap(sitemap_url: str) -> list[str]:
    response = requests.get(sitemap_url)
    response.raise_for_status()

    soup = BeautifulSoup(response.content, "html.parser")
    result = [
        _ensure_absolute_url(sitemap_url, loc_tag.text)
        for loc_tag in soup.find_all("loc")
    ]
    if not result:
        raise ValueError(
            f"No URLs found in sitemap {sitemap_url}. Try using the 'single' or 'recursive' scraping options instead."
        )

    return result


def _ensure_absolute_url(source_url: str, maybe_relative_url: str) -> str:
    if not urlparse(maybe_relative_url).netloc:
        return urljoin(source_url, maybe_relative_url)
    return maybe_relative_url


def _ensure_valid_url(url: str) -> str:
    if "://" not in url:
        return "https://" + url
    return url


def _read_urls_file(location: str) -> list[str]:
    with open(location, "r") as f:
        urls = [_ensure_valid_url(line.strip()) for line in f if line.strip()]
    return urls


def extract_urls_from_sitemap_for_polling(
    sitemap_url: str, base_url: str
) -> list[tuple[str, datetime | None]]:
    """Extract URLs from sitemap specifically for polling updates."""
    response = requests.get(sitemap_url)
    response.raise_for_status()

    soup = BeautifulSoup(response.content, "html.parser")
    result = []

    # Process URLs from the sitemap
    for url_tag in soup.find_all("url"):
        loc_tag = url_tag.find("loc")
        lastmod_tag = url_tag.find("lastmod")

        if not loc_tag:
            continue

        url = _ensure_absolute_url(sitemap_url, loc_tag.text)
        # Only include URLs that match our base_url
        if base_url not in url:
            continue

        lastmod = None
        if lastmod_tag:
            try:
                lastmod = datetime.strptime(lastmod_tag.text, "%Y-%m-%dT%H:%M:%SZ")
                lastmod = lastmod.replace(tzinfo=timezone.utc)
            except ValueError:
                logger.warning(f"Could not parse lastmod date: {lastmod_tag.text}")

        result.append((url, lastmod))

    if not result:
        raise ValueError(
            f"No URLs found in sitemap {sitemap_url} matching base URL {base_url}."
        )

    return result


def get_sitemap_url_from_base_url(base_url: str) -> str:
    """Extract the product name from the base URL and construct the sitemap URL.

    Args:
        base_url: The base URL containing the product name (e.g. https://docs.uipath.com/ai-center/...)

    Returns:
        The constructed sitemap URL

    Raises:
        ValueError: If the base URL is not a valid UiPath docs URL
    """
    if "docs.uipath.com" not in base_url:
        raise ValueError("Base URL must be a UiPath docs URL")

    # Parse the URL to get the path
    parsed_url = urlparse(base_url)
    path_parts = parsed_url.path.strip("/").split("/")

    if not path_parts:
        raise ValueError("Could not extract product name from URL")

    # The product name is typically the first part after docs.uipath.com
    product_name = path_parts[0]

    sitemap_url = (
        f"https://docs.uipath.com/products/{product_name}/sitemaps/en/sitemap.xml"
    )
    return sitemap_url


# Concrete UiPath docs version segment, e.g. "2022.10", "2023.4", "2.2510".
# Deliberately matches only numeric dotted versions so the "latest" alias and
# non-version path segments are excluded.
_UIPATH_VERSION_RE = re.compile(r"^\d+(?:\.\d+)+$")


def _uipath_product_prefix(path: str) -> str:
    """The path up to (but excluding) the first version-or-'latest' segment.

    /robot/standalone/latest                       -> /robot/standalone
    /robot/standalone/2022.10                       -> /robot/standalone
    /apps/automation-suite/                         -> /apps/automation-suite
    /automation-cloud/automation-cloud/latest/x/y   -> /automation-cloud/automation-cloud
    """
    segs = [s for s in path.strip("/").split("/") if s]
    cut = len(segs)
    for i, seg in enumerate(segs):
        if seg == "latest" or _UIPATH_VERSION_RE.match(seg):
            cut = i
            break
    return "/" + "/".join(segs[:cut])


def get_uipath_docs_version_base_urls(base_url: str, max_versions: int = 3) -> list[str]:
    """Expand a docs.uipath.com product URL to the base URLs of its latest N
    concrete versions.

    docs.uipath product pages render a version selector (newest-first) linking
    each available version, e.g. /robot/standalone/2025.10. On-prem / standalone
    products list many concrete versions; cloud / evergreen products expose only
    the 'latest' alias. We take the first `max_versions` CONCRETE versions in the
    selector's own order — the site already sorts newest-first, which correctly
    handles the calendar -> MAJOR.YYMM scheme change (e.g. 2.2510 is newer than
    2023.10, which no naive numeric sort would get right) — skipping 'latest'.

    Returns, fail-safe:
      - the latest N concrete version base URLs for versioned products, else
      - [base_url] unchanged for non-docs.uipath.com URLs, evergreen products
        with no concrete versions, or any fetch/parse error.
    """
    if "docs.uipath.com" not in base_url:
        return [base_url]

    try:
        prefix = _uipath_product_prefix(urlparse(base_url).path)
        prefix_segs = [s for s in prefix.strip("/").split("/") if s]

        response = requests.get(base_url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        # Collect version-root links in document order (the selector is
        # newest-first), deduped: exactly the product prefix + one concrete
        # version segment (no deeper path -> isolates selector links from
        # content links).
        versions: list[str] = []
        for link in cast(list[dict[str, Any]], soup.find_all("a")):
            href = cast(str | None, link.get("href"))
            if not href:
                continue
            parsed = urlparse(urljoin(base_url, href.split("#")[0]))
            if parsed.netloc != "docs.uipath.com":
                continue
            segs = [s for s in parsed.path.strip("/").split("/") if s]
            if (
                len(segs) == len(prefix_segs) + 1
                and segs[: len(prefix_segs)] == prefix_segs
                and _UIPATH_VERSION_RE.match(segs[-1])
                and segs[-1] not in versions
            ):
                versions.append(segs[-1])

        if not versions:
            # evergreen / no multi-version selector -> crawl the URL as-is
            return [base_url]

        latest = versions[:max_versions]
        result = [f"https://docs.uipath.com{prefix}/{v}" for v in latest]
        logger.info(
            f"docs.uipath versioned product '{prefix}': crawling latest "
            f"{len(result)} of {len(versions)} versions {latest}"
        )
        return result
    except Exception as e:
        logger.warning(
            f"Could not expand UiPath docs versions for {base_url} ({e}); "
            "crawling the configured URL as-is."
        )
        return [base_url]


class WebConnector(LoadConnector, PollConnector):
    def __init__(
        self,
        base_url: str,  # Can't change this without disrupting existing users
        web_connector_type: str = WEB_CONNECTOR_VALID_SETTINGS.RECURSIVE.value,
        mintlify_cleanup: bool = True,  # Mostly ok to apply to other websites as well
        batch_size: int = INDEX_BATCH_SIZE,
        # OPT-IN, docs.uipath.com + RECURSIVE only. When True, the configured
        # base_url is expanded to the latest `max_versions` concrete versions of
        # that product (via the page's version selector), re-evaluated every
        # indexing run so new releases are auto-picked-up and the oldest drops
        # off. Leave False (default) for every other connector — auto-applying
        # would make per-version connectors all crawl the same latest set.
        uipath_latest_versions: bool = False,
        max_versions: int = 3,
    ) -> None:
        self.base_url = base_url
        self.mintlify_cleanup = mintlify_cleanup
        self.batch_size = batch_size
        self.recursive = False
        self.web_connector_type = web_connector_type
        self.uipath_latest_versions = uipath_latest_versions
        self.max_versions = max_versions
        # Recursion scope (RECURSIVE mode). Defaults to the configured base_url;
        # overridden below to the latest-N version base URLs when version
        # tracking is enabled for a versioned docs.uipath product.
        self.recursive_prefixes: list[str] = [base_url]

        if web_connector_type == WEB_CONNECTOR_VALID_SETTINGS.RECURSIVE.value:
            self.recursive = True
            if uipath_latest_versions:
                # docs.uipath versioned product -> latest N version base URLs;
                # returns [base_url] unchanged for evergreen / non-docs URLs.
                expanded = get_uipath_docs_version_base_urls(
                    base_url, max_versions=max_versions
                )
                self.to_visit_list = [_ensure_valid_url(u) for u in expanded]
                self.recursive_prefixes = self.to_visit_list
            else:
                self.to_visit_list = [_ensure_valid_url(base_url)]
            return

        elif web_connector_type == WEB_CONNECTOR_VALID_SETTINGS.SINGLE.value:
            self.to_visit_list = [_ensure_valid_url(base_url)]

        elif web_connector_type == WEB_CONNECTOR_VALID_SETTINGS.SITEMAP:
            self.to_visit_list = extract_urls_from_sitemap(_ensure_valid_url(base_url))

        elif web_connector_type == WEB_CONNECTOR_VALID_SETTINGS.UPLOAD:
            logger.warning(
                "This is not a UI supported Web Connector flow, "
                "are you sure you want to do this?"
            )
            self.to_visit_list = _read_urls_file(base_url)

        else:
            raise ValueError(
                "Invalid Web Connector Config, must choose a valid type between: " ""
            )

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        if credentials:
            logger.warning("Unexpected credentials provided for Web Connector")
        return None

    def load_from_state(self, is_polling: bool = False) -> GenerateDocumentsOutput:
        """Traverses through all pages found on the website
        and converts them into documents"""
        visited_links: set[str] = set()
        to_visit: list[str] = self.to_visit_list

        if not to_visit:
            raise ValueError("No URLs to visit")

        base_url = to_visit[0]  # For the recursive case
        doc_batch: list[Document] = []

        # Needed to report error
        at_least_one_doc = False
        last_error = None

        # One upfront connectivity check. This used to run per page — a full
        # extra GET for every URL (doubling network work) that ALSO 403'd on
        # bot-protected sites Playwright loads fine, and a failure tore down the
        # whole browser. Once, on the base URL, is enough.
        check_internet_connection(base_url)

        playwright, context = start_playwright()
        restart_playwright = False
        pages_visited = 0
        while to_visit:
            if WEB_CONNECTOR_MAX_PAGES and pages_visited >= WEB_CONNECTOR_MAX_PAGES:
                logger.info(
                    f"Reached WEB_CONNECTOR_MAX_PAGES ({WEB_CONNECTOR_MAX_PAGES}); "
                    f"stopping crawl with {len(to_visit)} URL(s) still queued."
                )
                break

            current_url = to_visit.pop()
            if current_url in visited_links:
                continue
            visited_links.add(current_url)

            try:
                protected_url_check(current_url)
            except Exception as e:
                last_error = f"Invalid URL {current_url} due to {e}"
                logger.warning(last_error)
                continue

            logger.info(f"Visiting {current_url}")
            pages_visited += 1

            # Reinit the browser if a previous batch/crash flagged it. Done for
            # every page (as before) so the browser is always live when we reach
            # the batch-yield / final-stop below.
            if restart_playwright:
                playwright, context = start_playwright()
                restart_playwright = False

            # --- PDF: fetch directly (no browser). timeout so a hung download
            # can't stall the whole attempt. Matches the original: PDFs don't
            # trigger the per-batch flush. ---
            if current_url.split(".")[-1] == "pdf":
                try:
                    response = requests.get(current_url, timeout=60)
                    response.raise_for_status()
                    page_text = pdf_to_text(file=io.BytesIO(response.content))
                    doc_batch.append(
                        Document(
                            id=current_url,
                            sections=[Section(link=current_url, text=page_text)],
                            source=DocumentSource.WEB,
                            semantic_identifier=current_url.split(".")[-1],
                            metadata={},
                        )
                    )
                except Exception as e:
                    last_error = f"Failed to fetch PDF '{current_url}': {e}"
                    logger.error(last_error)
                continue

            # --- HTML via Playwright, with retries. A single page error retries
            # with a FRESH PAGE on the same browser (exponential backoff); only
            # a browser-level crash restarts Playwright. One bad page no longer
            # tears down the browser or fails the attempt. ---
            page_doc: Document | None = None
            for attempt in range(WEB_CONNECTOR_MAX_RETRIES):
                if attempt > 0:
                    time.sleep(min(2**attempt + random.uniform(0, 1), 10))
                try:
                    page = context.new_page()
                    try:
                        page_response = page.goto(
                            current_url,
                            timeout=WEB_CONNECTOR_PAGE_TIMEOUT_MS,
                            # 'domcontentloaded' (DOM parsed) instead of the
                            # default 'load' (waits for every image/font/etc.) —
                            # far faster and enough for text extraction.
                            wait_until="domcontentloaded",
                        )
                        final_page = page.url
                        if final_page != current_url:
                            logger.info(f"Redirected to {final_page}")
                            protected_url_check(final_page)
                            current_url = final_page
                            if current_url in visited_links:
                                logger.info("Redirected page already indexed")
                                break
                            visited_links.add(current_url)

                        content = page.content()
                        soup = BeautifulSoup(content, "html.parser")

                        if self.recursive and not is_polling:
                            for link in get_internal_links(
                                base_url,
                                current_url,
                                soup,
                                allowed_prefixes=self.recursive_prefixes,
                            ):
                                if link not in visited_links:
                                    to_visit.append(link)

                        if page_response and str(page_response.status)[0] in (
                            "4",
                            "5",
                        ):
                            last_error = (
                                f"Skipped indexing {current_url} due to HTTP "
                                f"{page_response.status} response"
                            )
                            logger.info(last_error)
                            break  # a real 4xx/5xx — don't retry

                        parsed_html = web_html_cleanup(soup, self.mintlify_cleanup)
                        page_doc = Document(
                            id=current_url,
                            sections=[
                                Section(link=current_url, text=parsed_html.cleaned_text)
                            ],
                            source=DocumentSource.WEB,
                            semantic_identifier=parsed_html.title or current_url,
                            metadata={},
                        )
                        break  # success
                    finally:
                        page.close()
                except Exception as e:
                    last_error = (
                        f"Failed to fetch '{current_url}' "
                        f"(attempt {attempt + 1}/{WEB_CONNECTOR_MAX_RETRIES}): {e}"
                    )
                    logger.warning(last_error)
                    if _is_browser_dead(e):
                        # Browser/context crashed — restart it so the next
                        # attempt (and subsequent pages) have a live browser.
                        try:
                            playwright.stop()
                        except Exception:
                            pass
                        playwright, context = start_playwright()
                    # else: transient page error — retry with a fresh page.

            if page_doc is not None:
                doc_batch.append(page_doc)

            if len(doc_batch) >= self.batch_size:
                playwright.stop()
                restart_playwright = True
                at_least_one_doc = True
                yield doc_batch
                doc_batch = []

        if doc_batch:
            playwright.stop()
            at_least_one_doc = True
            yield doc_batch

        if not at_least_one_doc:
            if last_error:
                raise RuntimeError(last_error)
            raise RuntimeError("No valid pages found.")

    def poll_source(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch
    ) -> GenerateDocumentsOutput:
        start_datetime = datetime.fromtimestamp(start, tz=timezone.utc)
        end_datetime = datetime.fromtimestamp(end, tz=timezone.utc)

        try:
            # Only handle SINGLE and RECURSIVE cases for polling
            if self.web_connector_type not in [
                WEB_CONNECTOR_VALID_SETTINGS.SINGLE.value,
                WEB_CONNECTOR_VALID_SETTINGS.RECURSIVE.value,
            ]:
                logger.info(
                    f"Polling not supported for connector type {self.web_connector_type}"
                )
                return self.load_from_state(is_polling=False)

            # Only do polling for UiPath docs
            if "docs.uipath.com" not in self.base_url:
                logger.info(
                    "Polling only supported for UiPath docs, falling back to regular indexing"
                )
                return self.load_from_state(is_polling=False)

            urls_to_index = []

            # Use the product-specific sitemap for UiPath docs
            try:
                sitemap_url = get_sitemap_url_from_base_url(self.base_url)
            except ValueError as e:
                logger.warning(f"Failed to construct sitemap URL: {e}")
                return self.load_from_state(is_polling=False)

            # Extract URLs with their lastmod dates, filtering by base_url
            urls_with_dates = extract_urls_from_sitemap_for_polling(
                sitemap_url, self.base_url
            )

            # For single page, only check if the base URL has been modified
            if self.web_connector_type == WEB_CONNECTOR_VALID_SETTINGS.SINGLE.value:
                for url, lastmod in urls_with_dates:
                    if url == self.base_url:
                        if lastmod and start_datetime <= lastmod <= end_datetime:
                            urls_to_index.append(url)
                        elif not lastmod:
                            urls_to_index.append(url)
                        break
            else:  # RECURSIVE case
                for url, lastmod in urls_with_dates:
                    if lastmod and start_datetime <= lastmod <= end_datetime:
                        urls_to_index.append(url)
                    # If we don't have a lastmod date, we should check the page
                    elif not lastmod:
                        urls_to_index.append(url)

            if urls_to_index:
                logger.info(
                    f"Found {len(urls_to_index)} pages modified between {start_datetime} and {end_datetime}"
                )
                self.to_visit_list = urls_to_index
                return self.load_from_state(is_polling=True)
            else:
                logger.info("No pages modified in the specified time window")
                # Return empty result instead of doing full load_from_state
                return iter([])

        except Exception as e:
            logger.warning(f"Failed to use sitemap for polling: {e}")
            # Fall back to a full RECURSIVE crawl (is_polling=False) when the
            # sitemap is unavailable. The product sitemap URL is currently stale
            # (404s), so this fallback is the common path; with is_polling=True
            # recursion is disabled (see load_from_state) and only the seed
            # URL(s) would be indexed — i.e. just landing pages. A full crawl
            # also honors uipath_latest_versions (to_visit_list/recursive_prefixes
            # are version-expanded in __init__).
            return self.load_from_state(is_polling=False)


if __name__ == "__main__":
    connector = WebConnector("https://docs.danswer.dev/")
    document_batches = connector.load_from_state()
    print(next(document_batches))
