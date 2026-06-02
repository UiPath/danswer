import os
import re
from collections.abc import Iterable
from datetime import datetime
from datetime import timezone
from typing import Any

from jira import JIRA
from jira.resources import Issue

from danswer.configs.app_configs import INDEX_BATCH_SIZE
from danswer.configs.app_configs import JIRA_CONNECTOR_LABELS_TO_SKIP
from danswer.configs.app_configs import JIRA_CONNECTOR_MAX_TICKET_SIZE
from danswer.configs.constants import DocumentSource
from danswer.connectors.cross_connector_utils.miscellaneous_utils import time_str_to_utc
from danswer.connectors.danswer_jira.utils import best_effort_basic_expert_info
from danswer.connectors.danswer_jira.utils import best_effort_get_field_from_issue
from danswer.connectors.danswer_jira.utils import extract_text_from_content
from danswer.connectors.danswer_jira.utils import get_comment_strs
from danswer.connectors.interfaces import GenerateDocumentsOutput
from danswer.connectors.interfaces import IdConnector
from danswer.connectors.interfaces import LoadConnector
from danswer.connectors.interfaces import PollConnector
from danswer.connectors.interfaces import SecondsSinceUnixEpoch
from danswer.connectors.models import ConnectorMissingCredentialError
from danswer.connectors.models import Document
from danswer.connectors.models import Section
from danswer.utils.logger import setup_logger


logger = setup_logger()

JIRA_API_VERSION = os.environ.get("JIRA_API_VERSION") or "3"
_JIRA_FULL_PAGE_SIZE = 50

# Matches a top-level trailing ORDER BY clause (case-insensitive).
_JQL_ORDER_BY_RE = re.compile(r"\border\s+by\b", re.IGNORECASE)


def _add_time_window_to_jql(
    jira_filter: str, start_date_str: str, end_date_str: str
) -> str:
    """Add the poll's `updated` time window to a user-supplied JQL filter.

    JQL requires all WHERE conditions to come BEFORE any `ORDER BY`. Naively
    appending `AND updated >= ...` to a filter that ends in `ORDER BY ...`
    produces invalid JQL — Jira rejects it with HTTP 400 "Expecting ',' but got
    'AND'". So if the filter has a trailing ORDER BY, inject the window in front
    of it; otherwise just append.
    """
    window = f"updated >= '{start_date_str}' AND updated <= '{end_date_str}'"
    jira_filter = jira_filter.strip()

    match = _JQL_ORDER_BY_RE.search(jira_filter)
    if match:
        where_part = jira_filter[: match.start()].rstrip()
        order_part = jira_filter[match.start() :].strip()
        if where_part:
            return f"{where_part} AND {window} {order_part}"
        return f"{window} {order_part}"

    if jira_filter:
        return f"{jira_filter} AND {window}"
    return window


def _paginate_jql_search(
    jira_client: JIRA,
    jql: str,
    max_results: int,
    fields: str | None = None,
) -> Iterable[Issue]:
    # Use enhanced_search_issues for Jira Cloud (API v3)
    # It uses search tokens instead of startAt for pagination
    logger.debug(f"Fetching Jira issues with JQL: {jql}, max results: {max_results}")
    issues = jira_client.enhanced_search_issues(
        jql_str=jql,
        maxResults=max_results,
        fields=fields or "*all",
    )

    for issue in issues:
        if isinstance(issue, Issue):
            yield issue
        else:
            raise Exception(f"Found Jira object not of type Issue: {issue}")


def fetch_jira_issues_batch(
    jira_client: JIRA,
    jql: str,
    batch_size: int,
    comment_email_blacklist: tuple[str, ...] = (),
    labels_to_skip: set[str] | None = None,
) -> Iterable[Document]:
    for issue in _paginate_jql_search(
        jira_client=jira_client,
        jql=jql,
        max_results=batch_size,
    ):
        # Per-issue error tolerance: a single malformed issue (odd field shape,
        # missing data, etc.) should be logged and skipped, NOT abort the whole
        # connector run. Previously one bad ticket failed the entire attempt.
        issue_key = getattr(issue, "key", "<unknown>")
        try:
            if labels_to_skip and any(
                label in issue.fields.labels for label in labels_to_skip
            ):
                logger.info(
                    f"Skipping {issue_key} because it has a label to skip. Found "
                    f"labels: {issue.fields.labels}. Labels to skip: {labels_to_skip}."
                )
                continue

            description = (
                issue.fields.description or ""
                if JIRA_API_VERSION == "2"
                else extract_text_from_content(issue.raw["fields"].get("description"))
            )
            comments = get_comment_strs(
                issue=issue,
                comment_email_blacklist=comment_email_blacklist,
            )
            ticket_content = f"{description}\n" + "\n".join(
                [f"Comment: {comment}" for comment in comments if comment]
            )

            # Check ticket size
            if len(ticket_content.encode("utf-8")) > JIRA_CONNECTOR_MAX_TICKET_SIZE:
                logger.info(
                    f"Skipping {issue_key} because it exceeds the maximum size of "
                    f"{JIRA_CONNECTOR_MAX_TICKET_SIZE} bytes."
                )
                continue

            page_url = f"{jira_client.client_info()}/browse/{issue_key}"

            people = set()
            for role in ("creator", "reporter", "assignee"):
                try:
                    field_value = best_effort_get_field_from_issue(issue, role)
                    if basic_expert_info := best_effort_basic_expert_info(field_value):
                        people.add(basic_expert_info)
                except Exception:
                    # role may be absent on some issues; not critical
                    pass

            metadata_dict: dict[str, Any] = {}
            if priority := best_effort_get_field_from_issue(issue, "priority"):
                metadata_dict["priority"] = priority.name
            if status := best_effort_get_field_from_issue(issue, "status"):
                metadata_dict["status"] = status.name
            if resolution := best_effort_get_field_from_issue(issue, "resolution"):
                metadata_dict["resolution"] = resolution.name
            if labels := best_effort_get_field_from_issue(issue, "labels"):
                metadata_dict["label"] = labels
            if issuetype := best_effort_get_field_from_issue(issue, "issuetype"):
                metadata_dict["issuetype"] = issuetype.name
            if reporter := best_effort_get_field_from_issue(issue, "reporter"):
                if reporter_name := getattr(reporter, "displayName", None):
                    metadata_dict["reporter"] = reporter_name
            if project := best_effort_get_field_from_issue(issue, "project"):
                if project_key := getattr(project, "key", None):
                    metadata_dict["project"] = project_key

            doc = Document(
                id=page_url,
                sections=[Section(link=page_url, text=ticket_content)],
                source=DocumentSource.JIRA,
                semantic_identifier=f"{issue_key}: {issue.fields.summary}",
                title=f"{issue_key} {issue.fields.summary}",
                doc_updated_at=time_str_to_utc(issue.fields.updated),
                primary_owners=list(people) or None,
                # TODO add secondary_owners (commenters) if needed
                metadata=metadata_dict,
            )
        except Exception as e:
            logger.exception(
                f"Failed to process Jira issue {issue_key}, skipping it: {e}"
            )
            continue

        yield doc


class JiraConnector(LoadConnector, PollConnector, IdConnector):
    def __init__(
        self,
        jira_base_url: str,
        jira_filter: str,
        comment_email_blacklist: list[str] | None = None,
        batch_size: int = INDEX_BATCH_SIZE,
        # if a ticket has one of the labels specified in this list, we will just
        # skip it. This is generally used to avoid indexing extra sensitive
        # tickets.
        labels_to_skip: list[str] = JIRA_CONNECTOR_LABELS_TO_SKIP,
    ) -> None:
        self.batch_size = batch_size
        self.jira_base = jira_base_url
        self._jira_client: JIRA | None = None
        self._comment_email_blacklist = comment_email_blacklist or []

        self.labels_to_skip = set(labels_to_skip)
        self.jira_filter = jira_filter

    @property
    def comment_email_blacklist(self) -> tuple:
        return tuple(email.strip() for email in self._comment_email_blacklist)

    @property
    def jira_client(self) -> JIRA:
        if self._jira_client is None:
            raise ConnectorMissingCredentialError("Jira")
        return self._jira_client

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        api_token = credentials["jira_api_token"]
        # if user provide an email we assume it's cloud
        if "jira_user_email" in credentials:
            email = credentials["jira_user_email"]
            self._jira_client = JIRA(
                basic_auth=(email, api_token),
                server=self.jira_base,
                options={"rest_api_version": JIRA_API_VERSION},
            )
        else:
            self._jira_client = JIRA(
                token_auth=api_token,
                server=self.jira_base,
                options={"rest_api_version": JIRA_API_VERSION},
            )
        return None

    def load_from_state(self) -> GenerateDocumentsOutput:
        # Full (unbounded) load = the configured filter with no time window.
        # Previously this referenced self.quoted_jira_project, which __init__
        # never sets — an AttributeError on any call (notably the prune path,
        # which falls back to load_from_state for non-IdConnectors).
        jql = self.jira_filter

        document_batch = []
        for doc in fetch_jira_issues_batch(
            jira_client=self.jira_client,
            jql=jql,
            batch_size=_JIRA_FULL_PAGE_SIZE,
            comment_email_blacklist=self.comment_email_blacklist,
            labels_to_skip=self.labels_to_skip,
        ):
            document_batch.append(doc)
            if len(document_batch) >= self.batch_size:
                yield document_batch
                document_batch = []

        yield document_batch

    def poll_source(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch
    ) -> GenerateDocumentsOutput:
        if self.jira_client is None:
            raise ConnectorMissingCredentialError("Jira")

        start_date_str = datetime.fromtimestamp(start, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )
        end_date_str = datetime.fromtimestamp(end, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )

        jql = _add_time_window_to_jql(self.jira_filter, start_date_str, end_date_str)

        document_batch = []
        for doc in fetch_jira_issues_batch(
            jira_client=self.jira_client,
            jql=jql,
            batch_size=_JIRA_FULL_PAGE_SIZE,
            comment_email_blacklist=self.comment_email_blacklist,
            labels_to_skip=self.labels_to_skip,
        ):
            document_batch.append(doc)
            if len(document_batch) >= self.batch_size:
                yield document_batch
                document_batch = []

        yield document_batch

    def retrieve_all_source_ids(self) -> set[str]:
        """ID-only listing for the prune path. Returns the document ids (same
        `<base>/browse/<KEY>` form used at index time) for every issue matching
        the filter, fetching ONLY the `key` field. Implementing IdConnector lets
        pruning detect deleted issues cheaply, instead of loading every full
        document just to read its id (and instead of hitting the old
        load_from_state, which was broken)."""
        if self.jira_client is None:
            raise ConnectorMissingCredentialError("Jira")

        base = self.jira_client.client_info()
        all_ids: set[str] = set()
        for issue in _paginate_jql_search(
            jira_client=self.jira_client,
            jql=self.jira_filter,
            max_results=_JIRA_FULL_PAGE_SIZE,
            fields="key",
        ):
            all_ids.add(f"{base}/browse/{issue.key}")
        return all_ids


if __name__ == "__main__":
    import os

    connector = JiraConnector(os.environ["JIRA_FILTERS"], comment_email_blacklist=[])
    connector.load_credentials(
        {
            "jira_user_email": os.environ["JIRA_USER_EMAIL"],
            "jira_api_token": os.environ["JIRA_API_TOKEN"],
        }
    )
    document_batches = connector.load_from_state()
    print(next(document_batches))
