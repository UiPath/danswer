"""WebLink scraping helper for the Highspot connector.

Highspot Spot items can be of type WebLink — i.e. just a URL the
Spot owner curated. These have no API-side body content; we have to
go fetch the page and extract its readable text.

The Playwright lifecycle here mirrors `connectors/web/connector.py`:
the caller (HighspotConnector.poll_source) opens **one** browser /
context for the entire run and passes it in via `context=`. Each
WebLink scrape just opens a new page (lightweight tab) inside the
shared browser and closes it after use. This is critical: spawning a
fresh Chromium process per WebLink — as the upstream Onyx port does
— starves the worker's file descriptors / RAM and causes co-running
connectors (e.g. Slack) to fail with `IncompleteRead` mid-response.

A `context=None` fallback path is kept so the standalone smoke test
in connector.py's `__main__` still works and so isolated callers
(future code, retries) don't have to plumb a context through.
"""
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext
from playwright.sync_api import sync_playwright

from danswer.file_processing.html_utils import web_html_cleanup
from danswer.utils.logger import setup_logger

logger = setup_logger()

WEB_CONNECTOR_MAX_SCROLL_ATTEMPTS = 10
JAVASCRIPT_DISABLED_MESSAGE = "You have JavaScript disabled in your browser"
DEFAULT_TIMEOUT = 60000  # 60 seconds — used for initial page.goto only.

# Tight per-scroll networkidle budget. Some pages (analytics
# beacons, polling websockets, autoplay video) never reach
# networkidle, so a 60s wait × 20 iterations stalls a single
# WebLink for up to 20 minutes. Cap this at 5s so a stuck page
# costs at most ~25s total before we move on with whatever HTML
# we've already loaded.
SCROLL_NETWORKIDLE_TIMEOUT_MS = 5000


def _scrape_using_page(
    page: "object",  # playwright.sync_api.Page
    url: str,
    scroll_before_scraping: bool,
    timeout_ms: int,
) -> Optional[str]:
    """Inner scrape: navigate, optionally scroll, run html cleanup,
    iframe-fallback. Caller manages the page lifecycle."""
    logger.info("Navigating to URL: %s", url)
    try:
        page.goto(url, timeout=timeout_ms)
    except Exception as e:
        logger.error("Failed to navigate to %s: %s", url, str(e))
        return None

    if scroll_before_scraping:
        logger.debug("Scrolling page to load lazy content")
        scroll_attempts = 0
        previous_height = page.evaluate("document.body.scrollHeight")
        while scroll_attempts < WEB_CONNECTOR_MAX_SCROLL_ATTEMPTS:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            try:
                # Tight per-scroll budget — see comment on
                # SCROLL_NETWORKIDLE_TIMEOUT_MS. Some pages never
                # reach networkidle; we'd rather scrape what
                # loaded than hang the whole indexing run.
                page.wait_for_load_state(
                    "networkidle", timeout=SCROLL_NETWORKIDLE_TIMEOUT_MS
                )
            except Exception as e:
                logger.debug(
                    "Network idle wait timed out (continuing with current HTML): %s",
                    str(e),
                )
                break

            new_height = page.evaluate("document.body.scrollHeight")
            if new_height == previous_height:
                break
            previous_height = new_height
            scroll_attempts += 1

    content = page.content()
    soup = BeautifulSoup(content, "html.parser")

    parsed_html = web_html_cleanup(soup)

    if JAVASCRIPT_DISABLED_MESSAGE in parsed_html.cleaned_text:
        logger.debug("JavaScript disabled message detected, checking iframes")
        try:
            iframe_count = page.frame_locator("iframe").locator("html").count()
            if iframe_count > 0:
                iframe_texts = (
                    page.frame_locator("iframe").locator("html").all_inner_texts()
                )
                iframe_content = "\n".join(iframe_texts)

                if len(parsed_html.cleaned_text) < 700:
                    parsed_html.cleaned_text = iframe_content
                else:
                    parsed_html.cleaned_text += "\n" + iframe_content
        except Exception as e:
            logger.warning("Error processing iframes: %s", str(e))

    return parsed_html.cleaned_text


def scrape_url_content(
    url: str,
    scroll_before_scraping: bool = False,
    timeout_ms: int = DEFAULT_TIMEOUT,
    context: Optional[BrowserContext] = None,
) -> Optional[str]:
    """Scrape `url` via headless Chromium and return cleaned page
    text, or None on any failure.

    If `context` is provided (the production path from
    HighspotConnector.poll_source), a new page/tab is opened in that
    shared context and closed after use — no Chromium spawn cost.

    If `context` is None (the smoke-test path), this falls back to
    the spawn-per-call behavior so isolated callers still work. Do
    NOT use this fallback path inside the indexing loop: it costs a
    Chromium process per call and starves co-running connectors.
    """
    try:
        validate_url(url)
    except ValueError as e:
        logger.error("Invalid URL %s: %s", url, str(e))
        return None

    if context is not None:
        page = None
        try:
            page = context.new_page()
            return _scrape_using_page(
                page=page,
                url=url,
                scroll_before_scraping=scroll_before_scraping,
                timeout_ms=timeout_ms,
            )
        except Exception as e:
            logger.error("Error scraping URL %s: %s", url, str(e))
            return None
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception as e:
                    logger.debug("Error closing page: %s", str(e))
        # unreachable
        return None

    # Standalone path: spawn-then-teardown.
    playwright = None
    browser = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        context_local = browser.new_context()
        page = context_local.new_page()
        return _scrape_using_page(
            page=page,
            url=url,
            scroll_before_scraping=scroll_before_scraping,
            timeout_ms=timeout_ms,
        )
    except Exception as e:
        logger.error("Error scraping URL %s: %s", url, str(e))
        return None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception as e:
                logger.debug("Error closing browser: %s", str(e))
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as e:
                logger.debug("Error stopping playwright: %s", str(e))


def validate_url(url: str) -> None:
    """Validate that `url` has http(s) scheme + a hostname."""
    parse = urlparse(url)
    if parse.scheme != "http" and parse.scheme != "https":
        raise ValueError("URL must be of scheme https?://")

    if not parse.hostname:
        raise ValueError("URL must include a hostname")
