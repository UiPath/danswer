"""OutSystems connector.

Named generically for the OutSystems low-code platform; currently tuned to the
inside.uipath.com Intranet app (its module/screen names are baked into the
screenservice paths below — `Intranet`/`PageTemplates.Page`). To support another
OutSystems app later, parameterize those paths via connector config; the auth +
extraction logic stays the same.

INTERIM AUTH — to be enhanced once a service account exists. The target has no
API key / bearer token; the SPA authenticates with the SSO **session cookie**
plus an `x-csrftoken` header. So the credential here is currently a short-lived
browser session (cookie + csrf + apiVersion) captured from DevTools.

Because that session expires in hours, this is intended for ONE-TIME indexing:
create the connector with no refresh schedule (refresh_freq=None) and trigger a
single index run while the cookie is fresh. The content is mostly static, so one
backfill is sufficient until the service account lands — at which point the
credential (and `_auth_headers`) swap to the service account with no change to
the enumeration/extraction logic below.

Content model: pages are `/Page?PageId=<N>` (sequential ints). Each page's
content is fetched from a JSON "screenservice" and is a tree of
sections -> widgets. Widget items (`PageWidgetItem`) come in two flavors,
discriminated by `PageFileId`:
  - text widget   (PageFileId == 0): `Text1` is HTML body content.
  - document widget (PageFileId != 0): `Text1` is a FilePath (e.g.
    "IC_Content/Docs/foo.pdf"), `Text2` is the filename. The actual file lives
    in SharePoint; we resolve it via the `ActionFileMetadata_Get` screenservice
    -> a pre-authed `DownloadURL` (tempauth in the URL) -> download bytes ->
    extract text (PDF/DOCX/...). Many policy pages are near-empty text with the
    real content in the attached PDF, so file extraction is essential here.

File download needs the W_Document action's own apiVersion (separate from the
page action's, and server-enforced), supplied as `outsystems_file_api_version`.
If that credential is absent, file download is skipped (page text only).
"""
import html
import io
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from html.parser import HTMLParser
from typing import Any

import requests

from danswer.configs.app_configs import INDEX_BATCH_SIZE
from danswer.configs.constants import DocumentSource
from danswer.connectors.interfaces import GenerateDocumentsOutput
from danswer.connectors.interfaces import LoadConnector
from danswer.connectors.models import ConnectorMissingCredentialError
from danswer.connectors.models import Document
from danswer.connectors.models import Section
from danswer.file_processing.extract_file_text import extract_file_text
from danswer.utils.logger import setup_logger

logger = setup_logger()

_PAGE_DATA_ACTION = "/screenservices/Intranet/New/N_Page_Preview/DataActionGetPage"
_FILE_METADATA_ACTION = (
    "/screenservices/Intranet/PageWidgets/W_Document/ActionFileMetadata_Get"
)
_MODULE_VERSION_URL = "/moduleservices/moduleversioninfo"
_PAGE_VIEW_NAME = "PageTemplates.Page"
_DEFAULT_BASE_URL = "https://inside.uipath.com"
# (connect, read) timeouts on every outbound request. Without these a single
# hung connection freezes the whole index attempt indefinitely; with them a slow
# page/file times out, is logged, and skipped so the scan continues.
_HTTP_TIMEOUT = (10, 60)
# Wall-clock bound on a single file's text extraction, enforced in a SEPARATE
# PROCESS (a thread can't be force-killed when the parser holds the GIL). On
# timeout the child is terminated and the file skipped.
_FILE_EXTRACT_TIMEOUT = 120
# Skip files larger than this. Learned the hard way: a 249 MB .mp4 and several
# 4–15 MB PDFs froze the indexing pipeline (huge text -> thousands of embedding
# chunks). Big media has no useful text anyway; big PDFs are capped below too.
_MAX_FILE_BYTES = 20 * 1024 * 1024
# Per-file/-page text ceiling (generous — splitting below keeps it indexable).
_MAX_DOC_CHARS = 1_000_000
# Split a document's text into sections of at most this many chars. The chunker
# tokenizes section-by-section, and the HF tokenizer is ~O(n^2) on a single huge
# string — so one giant section freezes the worker. Bounded sections keep every
# tokenize() call small (linear overall) AND preserve all content (no skip, no
# truncation beyond the ceiling above).
_MAX_SECTION_CHARS = 30_000
# Any unbroken run of non-whitespace longer than this gets spaces inserted. PDFs
# of dense tables extract as enormous space-free strings; the HF tokenizer is
# ~O(n^2) per "word", so a 500K-char run tokenizes for MINUTES, holding the GIL
# and freezing the dask worker. Breaking long runs makes tokenization linear.
_MAX_TOKEN_RUN = 80
# Pages whose attached file produces enormous text (151K–1.8M chars: drug
# formularies, clinic-network spreadsheets, multi-plan benefit PDFs). Such a doc
# tokenizes for minutes (~O(n^2)), holding the GIL and freezing the dask worker,
# which can't force-kill it (daemonic process -> no subprocess). Skipped entirely
# so a full sync can't stall. This is the COMPLETE list from a full 1..2200 scan
# (extract every file, measure chars; threshold 150K). Re-run scratchpad
# full_scan.py and refresh if the source content changes materially.
# Pages whose attached file is enormous (151K–1.8M chars: drug formularies,
# clinic-network spreadsheets, multi-plan benefit PDFs). Even split into bounded
# sections these stall the chunk/embed stage — each explodes into ~1.5K chunks
# that the CPU-only embedder can't process in reasonable time (verified: a split
# run upserted the rows then hung in chunk/embed with the model server idle).
# Section-splitting (below) still protects against *moderately* large docs; this
# list is the small set of genuinely-unindexable giants. Complete list from a
# full 1..2200 scan (scratchpad/full_scan.py, char threshold 150K). To actually
# index these later, use a GPU embedder or ingest them split out-of-band.
_SKIP_PAGE_IDS: set[int] = {
    585, 833, 835, 850, 901, 906, 907, 908, 940, 953,
    1139, 1193, 1197, 1209, 1591, 1684, 1818, 1820, 2090, 2095,
}
# Minimum extracted-text length for a page to be treated as real content (skips
# empty / unpublished / widget-only pages). Computed over page text + file text.
_MIN_TEXT_LEN = 40
# Only attempt download/extract for file types extract_file_text handles.
_DOC_EXTENSIONS = (
    ".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".eml", ".epub", ".html", ".txt",
)
# Explicitly skip videos/media/archives — no useful text, and the originals are
# huge (e.g. a 249 MB onboarding .mp4, many ~10 MB .webm walkthroughs). Skipped
# with a log so it's auditable, before any download. (The allowlist above would
# drop them too; this makes the intent explicit and visible.)
_MEDIA_EXTENSIONS = (
    ".mp4", ".webm", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".m4v",
    ".mp3", ".wav", ".m4a", ".aac", ".ogg",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".webp",
    ".zip", ".rar", ".7z", ".tar", ".gz",
)


class _TextExtractor(HTMLParser):
    """Strip HTML to readable text, inserting newlines around block elements."""

    _BLOCK = {
        "p", "div", "br", "li", "ul", "ol", "tr", "td", "th",
        "h1", "h2", "h3", "h4", "h5", "h6", "section", "table",
    }

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        joined = html.unescape("".join(self._parts))
        lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in joined.splitlines()]
        out: list[str] = []
        for ln in lines:
            if ln or (out and out[-1]):
                out.append(ln)
        return "\n".join(out).strip()


def _strip_html(raw: str) -> str:
    parser = _TextExtractor()
    parser.feed(raw or "")
    return parser.text()


def _break_long_tokens(text: str, n: int = _MAX_TOKEN_RUN) -> str:
    """Insert a space into any whitespace-free run longer than `n` chars. Real
    words are short; only pathological blobs (table dumps, base64) get broken,
    which keeps the tokenizer linear without losing readable content."""
    if not text:
        return text
    return re.sub(r"\S{%d}" % n, lambda m: m.group(0) + " ", text)


def _split_sections(text: str, link: str, header: str | None = None) -> list[Section]:
    """Turn one (possibly huge) text into several bounded-size Sections so the
    chunker never tokenizes a giant string in one call. Splits on a newline near
    each boundary when possible. `header` (e.g. a filename) prefixes the text."""
    text = (text or "").strip()
    if not text:
        return []
    text = text[:_MAX_DOC_CHARS]
    if header:
        text = f"{header}\n\n{text}"
    out: list[Section] = []
    i, n = 0, len(text)
    while i < n:
        end = min(i + _MAX_SECTION_CHARS, n)
        if end < n:  # prefer a newline boundary in the back half of the window
            nl = text.rfind("\n", i + _MAX_SECTION_CHARS // 2, end)
            if nl != -1:
                end = nl
        chunk = text[i:end].strip()
        if chunk:
            out.append(Section(link=link, text=chunk))
        i = end
    return out


def _extract_text_with_timeout(filename: str, content: bytes, timeout: int) -> str:
    """Run extract_file_text under a soft wall-clock bound on a worker thread.

    NOTE: must be threads, not a subprocess — the dask indexing worker runs tasks
    in a *daemonic* process, which cannot spawn children ("daemonic processes are
    not allowed to have children"), so multiprocessing is unavailable here. On
    timeout we abandon the thread (can't force-kill it) and return "" so the run
    keeps moving. This is a backstop only: the real protection against the giant
    documents that froze prod is the file-size + text caps below, which bound the
    download and the embedding work in-process."""
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(
            extract_file_text,
            file_name=filename,
            file=io.BytesIO(content),
            break_on_unprocessable=False,
        )
        return fut.result(timeout=timeout)
    except FutureTimeout:
        logger.warning(f"OutSystems extract timed out after {timeout}s: {filename}")
        return ""
    except Exception as e:
        logger.warning(f"OutSystems extract error for {filename}: {e}")
        return ""
    finally:
        ex.shutdown(wait=False)  # never block on a hung extraction


def collect_widgets(obj: Any, html_blobs: list[str], files: list[tuple[str, str]]) -> None:
    """Walk the page response and classify each PageWidgetItem:
      - text widget   (PageFileId falsy/"0"): Text1 HTML -> html_blobs
      - document widget (PageFileId truthy)  : (Text1 FilePath, Text2 filename) -> files
    Keyed on the widget-item shape (a dict carrying both Text1 and PageFileId),
    so a document widget's Text1 (a file path) is NOT mistaken for body text.
    """
    if isinstance(obj, dict):
        if "Text1" in obj:
            file_id = str(obj.get("PageFileId") or "0")
            text1 = obj.get("Text1")
            if file_id and file_id != "0":
                filepath = (text1 or "").strip()
                filename = (obj.get("Text2") or "").strip() or filepath.split("/")[-1]
                if filepath:
                    files.append((filepath, filename))
            elif isinstance(text1, str) and text1.strip():
                html_blobs.append(text1)
            # still recurse (widget items don't nest other widget items, but
            # be safe / cheap)
        for v in obj.values():
            collect_widgets(v, html_blobs, files)
    elif isinstance(obj, list):
        for v in obj:
            collect_widgets(v, html_blobs, files)


def extract_page_text(data: dict) -> str:
    """Plain text from the page's text widgets only (no file content)."""
    html_blobs: list[str] = []
    files: list[tuple[str, str]] = []
    collect_widgets(data, html_blobs, files)
    return "\n\n".join(_strip_html(b) for b in html_blobs if b).strip()


def extract_page_title(data: dict, body_text: str, page_id: int) -> str:
    name = (data.get("Page2") or {}).get("Name") or ""
    if name.strip():
        return name.strip()
    for line in body_text.splitlines():
        if len(line.strip()) >= 3:
            return line.strip()[:120]
    return f"OutSystems Page {page_id}"


class OutSystemsConnector(LoadConnector):
    def __init__(
        self,
        page_id_start: int = 1,
        page_id_end: int = 600,
        # Deliberately small (vs the default INDEX_BATCH_SIZE of 16). These pages
        # carry large PDFs -> large text -> many embedding chunks per doc. Big
        # batches made a single index_doc_batch tokenize/embed for minutes,
        # holding the worker GIL until the run looked frozen. A small batch keeps
        # each chunk/embed unit bounded so the worker stays responsive and the
        # full range completes in one pass.
        batch_size: int = 4,
    ) -> None:
        self.page_id_start = int(page_id_start)
        self.page_id_end = int(page_id_end)
        self.batch_size = batch_size

        self.base_url = _DEFAULT_BASE_URL
        self._cookie: str | None = None
        self._csrf: str | None = None
        self._api_version: str | None = None
        self._file_api_version: str | None = None
        self._module_version: str | None = None

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        self.base_url = (
            credentials.get("outsystems_base_url") or _DEFAULT_BASE_URL
        ).rstrip("/")
        self._cookie = credentials.get("outsystems_cookie")
        self._csrf = credentials.get("outsystems_csrf")
        self._api_version = credentials.get("outsystems_api_version")
        # Optional: enables downloading attached files (PDFs etc). Absent -> page
        # text only. Server-enforced and distinct from the page apiVersion.
        self._file_api_version = credentials.get("outsystems_file_api_version")
        return None

    def _auth_headers(self) -> dict[str, str]:
        # Swap point for the service account later. Never logged.
        return {
            "cookie": self._cookie or "",
            "x-csrftoken": self._csrf or "",
            "content-type": "application/json; charset=UTF-8",
            "accept": "application/json",
            "referer": self.base_url + "/",
            "origin": self.base_url,
        }

    def _session(self) -> requests.Session:
        if not self._cookie or not self._csrf:
            raise ConnectorMissingCredentialError("OutSystems")
        s = requests.Session()
        s.headers.update(self._auth_headers())
        return s

    def _fetch_module_version(self, s: requests.Session) -> str:
        if self._module_version:
            return self._module_version
        r = s.get(self.base_url + _MODULE_VERSION_URL, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        token = r.json().get("versionToken", "")
        if not token:
            raise RuntimeError("Could not read moduleversioninfo.versionToken")
        self._module_version = token
        return token

    def _fetch_page(self, s: requests.Session, page_id: int) -> dict | None:
        payload = {
            "versionInfo": {
                "moduleVersion": self._module_version,
                "apiVersion": self._api_version,
            },
            "viewName": _PAGE_VIEW_NAME,
            "screenData": {
                "variables": {
                    "PageId": str(page_id),
                    "_pageIdInDataFetchStatus": 1,
                    "PageTypeId": 1,
                    "_pageTypeIdInDataFetchStatus": 1,
                    "SubSiteId": "0",
                    "_subSiteIdInDataFetchStatus": 1,
                    "ShowShareButton": False,
                    "_showShareButtonInDataFetchStatus": 1,
                }
            },
        }
        try:
            r = s.post(
                self.base_url + _PAGE_DATA_ACTION, json=payload, timeout=_HTTP_TIMEOUT
            )
        except requests.RequestException as e:
            # Hung / slow / dropped connection: skip this page so one bad page
            # never freezes the whole scan. It can be re-picked on a later run.
            logger.warning(f"OutSystems page {page_id}: request failed ({e}); skipping")
            return None
        if r.status_code in (401, 403):
            # Session expired mid-run. There's no cursor, so the page range IS
            # the resume point: re-run with page_id_start=<this page> (and a
            # fresh credential) to continue without re-doing earlier pages.
            raise ConnectorMissingCredentialError(
                f"OutSystems session expired at PageId {page_id} — refresh the "
                f"credential and re-run with page_id_start={page_id} to resume"
            )
        if r.status_code != 200:
            logger.warning(f"OutSystems page {page_id}: HTTP {r.status_code}")
            return None
        return r.json().get("data")

    def _file_download_url(self, s: requests.Session, filepath: str) -> str | None:
        """Resolve a widget FilePath to a pre-authed SharePoint DownloadURL via
        the W_Document screenservice. Returns None if disabled or unresolved."""
        if not self._file_api_version:
            return None
        payload = {
            "versionInfo": {
                "moduleVersion": self._module_version,
                "apiVersion": self._file_api_version,
            },
            "viewName": _PAGE_VIEW_NAME,
            "inputParameters": {
                "FilePath": filepath,
                "IgnoreMediaRefreshToken": False,
                "ForceRefresh": False,
                "AzureId": "",
            },
        }
        r = s.post(
            self.base_url + _FILE_METADATA_ACTION, json=payload, timeout=_HTTP_TIMEOUT
        )
        if r.status_code != 200:
            logger.warning(f"OutSystems file metadata {filepath}: HTTP {r.status_code}")
            return None
        return ((r.json().get("data") or {}).get("FileMetadata") or {}).get(
            "DownloadURL"
        ) or None

    def _download_file_text(
        self, s: requests.Session, filepath: str, filename: str
    ) -> str:
        """Download an attached file and extract its text. Best-effort: any
        failure logs and returns "" so one bad file never sinks the page."""
        low = filename.lower()
        if low.endswith(_MEDIA_EXTENSIONS):
            logger.info(f"OutSystems skip video/media (no text): {filename}")
            return ""
        if not low.endswith(_DOC_EXTENSIONS):
            logger.info(f"OutSystems skip unsupported file type: {filename}")
            return ""
        try:
            url = self._file_download_url(s, filepath)
            if not url:
                return ""
            # NOTE: the DownloadURL carries its own tempauth; do NOT send the
            # inside.uipath.com session cookie to sharepoint.com — use a bare
            # request so no cross-domain credential leak occurs. Stream so we can
            # bail on oversized files without buffering them fully.
            fr = requests.get(url, timeout=_HTTP_TIMEOUT, stream=True)
            if fr.status_code != 200:
                logger.warning(f"OutSystems download {filename}: HTTP {fr.status_code}")
                return ""
            declared = int(fr.headers.get("content-length") or 0)
            if declared > _MAX_FILE_BYTES:
                logger.info(
                    f"OutSystems skip oversized file {filename} "
                    f"({declared // 1048576} MB > {_MAX_FILE_BYTES // 1048576} MB)"
                )
                fr.close()
                return ""
            # Read with a hard byte cap (covers missing/false Content-Length).
            chunks: list[bytes] = []
            total = 0
            for piece in fr.iter_content(chunk_size=262144):
                chunks.append(piece)
                total += len(piece)
                if total > _MAX_FILE_BYTES:
                    logger.info(f"OutSystems skip oversized file {filename} (stream cap)")
                    fr.close()
                    return ""
            content = b"".join(chunks)
            text = _extract_text_with_timeout(
                filename, content, _FILE_EXTRACT_TIMEOUT
            )
            # Break pathological long runs (linear tokenization) then cap so a
            # huge doc can't explode into thousands of chunks and stall embedding.
            return _break_long_tokens(text.strip())[:_MAX_DOC_CHARS]
        except Exception as e:
            logger.warning(f"OutSystems file extract failed/skipped for {filename}: {e}")
            return ""

    def load_from_state(self) -> GenerateDocumentsOutput:
        s = self._session()
        self._fetch_module_version(s)

        batch: list[Document] = []
        for page_id in range(self.page_id_start, self.page_id_end + 1):
            # Progress breadcrumb: if the session expires, the logs show how far
            # we got so the run can be resumed via page_id_start.
            if page_id % 50 == 0:
                logger.info(f"OutSystems: scanning PageId {page_id}/{self.page_id_end}")
            if page_id in _SKIP_PAGE_IDS:
                logger.info(f"OutSystems: skipping known problem PageId {page_id}")
                continue
            data = self._fetch_page(s, page_id)
            if not data:
                continue

            html_blobs: list[str] = []
            file_refs: list[tuple[str, str]] = []
            collect_widgets(data, html_blobs, file_refs)
            page_text = _break_long_tokens(
                "\n\n".join(_strip_html(b) for b in html_blobs if b).strip()
            )

            url = f"{self.base_url}/Page?PageId={page_id}"
            # Split each text into bounded sections so the chunker can't choke on
            # a giant single string (keeps all content; no truncation/skip).
            sections: list[Section] = _split_sections(page_text, url)
            for filepath, filename in file_refs:
                file_text = self._download_file_text(s, filepath, filename)
                if file_text:
                    sections.extend(_split_sections(file_text, url, header=filename))

            total_len = sum(len(sec.text) for sec in sections)

            # Skip pages with no usable content (neither page text nor files).
            if total_len < _MIN_TEXT_LEN or not sections:
                continue

            title = extract_page_title(data, page_text, page_id)
            batch.append(
                Document(
                    id=f"OUTSYSTEMS__page_{page_id}",
                    sections=sections,
                    source=DocumentSource.OUTSYSTEMS,
                    semantic_identifier=title,
                    title=title,
                    metadata={"page_id": str(page_id)},
                )
            )
            if len(batch) >= self.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch
