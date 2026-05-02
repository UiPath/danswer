import time
from collections.abc import Iterator
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import cast

from github import Github
from github import GithubException
from github import RateLimitExceededException
from github import Repository
from github.Issue import Issue
from github.NamedUser import NamedUser
from github.PaginatedList import PaginatedList
from github.PullRequest import PullRequest

from danswer.configs.app_configs import GITHUB_CONNECTOR_BASE_URL
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
from danswer.utils.batching import batch_generator
from danswer.utils.logger import setup_logger


logger = setup_logger()


_MAX_NUM_RATE_LIMIT_RETRIES = 5
_ITEMS_PER_PAGE = 100  # GitHub max; defaults to 30 if unset


def _sleep_after_rate_limit_exception(github_client: Github) -> None:
    sleep_time = github_client.get_rate_limit().core.reset.replace(
        tzinfo=timezone.utc
    ) - datetime.now(tz=timezone.utc)
    sleep_time += timedelta(minutes=1)  # add an extra minute just to be safe
    sleep_seconds = max(0, int(sleep_time.total_seconds()))
    logger.info(f"Ran into Github rate-limit. Sleeping {sleep_seconds} seconds.")
    time.sleep(sleep_seconds)


def _get_batch_rate_limited(
    git_objs: PaginatedList, page_num: int, github_client: Github, attempt_num: int = 0
) -> list[Any]:
    if attempt_num > _MAX_NUM_RATE_LIMIT_RETRIES:
        raise RuntimeError(
            "Re-tried fetching batch too many times. Something is going wrong with fetching objects from Github"
        )

    try:
        objs = list(git_objs.get_page(page_num))
        # fetch all data here to disable lazy loading later
        # this is needed to capture the rate limit exception here (if one occurs)
        for obj in objs:
            if hasattr(obj, "raw_data"):
                getattr(obj, "raw_data")
        return objs
    except RateLimitExceededException:
        _sleep_after_rate_limit_exception(github_client)
        return _get_batch_rate_limited(
            git_objs, page_num, github_client, attempt_num + 1
        )


def _batch_github_objects(
    git_objs: PaginatedList, github_client: Github, batch_size: int
) -> Iterator[list[Any]]:
    page_num = 0
    while True:
        batch = _get_batch_rate_limited(git_objs, page_num, github_client)
        page_num += 1

        if not batch:
            break

        for mini_batch in batch_generator(batch, batch_size=batch_size):
            yield mini_batch


def _safe_user_login(user: NamedUser | None) -> str | None:
    if user is None:
        return None
    try:
        return user.login
    except GithubException:
        return None


def _basic_expert_from_user(user: NamedUser | None) -> BasicExpertInfo | None:
    """Build a BasicExpertInfo from a github user, with light tolerance for
    missing fields (deleted users, ghost commits)."""
    if user is None:
        return None
    try:
        login = user.login
    except GithubException:
        return None
    name: str | None = None
    email: str | None = None
    try:
        name = user.name
    except GithubException:
        pass
    try:
        email = user.email
    except GithubException:
        pass
    return BasicExpertInfo(display_name=name or login, email=email)


def _utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _convert_pr_to_document(pull_request: PullRequest) -> Document:
    metadata: dict[str, str | list[str]] = {
        "object_type": "PullRequest",
        "number": str(pull_request.number),
        "state": pull_request.state,
        "merged": str(pull_request.merged),
        "num_commits": str(pull_request.commits),
        "num_files_changed": str(pull_request.changed_files),
    }
    if author := _safe_user_login(pull_request.user):
        metadata["author"] = author
    if merged_by := _safe_user_login(pull_request.merged_by):
        metadata["merged_by"] = merged_by
    if assignees := [
        login
        for login in (_safe_user_login(a) for a in pull_request.assignees)
        if login
    ]:
        metadata["assignees"] = assignees
    if labels := [label.name for label in pull_request.labels]:
        metadata["labels"] = labels
    if pull_request.base and pull_request.base.repo:
        metadata["repo"] = pull_request.base.repo.full_name
    if created := _utc(pull_request.created_at):
        metadata["created_at"] = created
    if closed := _utc(pull_request.closed_at):
        metadata["closed_at"] = closed
    if merged_at := _utc(pull_request.merged_at):
        metadata["merged_at"] = merged_at

    primary_owner = _basic_expert_from_user(pull_request.user)

    return Document(
        id=pull_request.html_url,
        sections=[Section(link=pull_request.html_url, text=pull_request.body or "")],
        source=DocumentSource.GITHUB,
        semantic_identifier=f"{pull_request.number}: {pull_request.title}",
        # updated_at is UTC time but is timezone unaware, explicitly add UTC
        # as there is logic in indexing to prevent wrong timestamped docs
        # due to local time discrepancies with UTC
        doc_updated_at=pull_request.updated_at.replace(tzinfo=timezone.utc),
        primary_owners=[primary_owner] if primary_owner else None,
        metadata=metadata,
    )


def _fetch_issue_comments(issue: Issue) -> str:
    try:
        comments = issue.get_comments()
        return "\nComment: ".join(comment.body for comment in comments if comment.body)
    except GithubException as e:
        logger.warning(f"Failed to fetch comments for issue #{issue.number}: {e}")
        return ""


def _convert_issue_to_document(issue: Issue) -> Document:
    body = issue.body or ""
    comments_text = _fetch_issue_comments(issue)
    if comments_text:
        full_text = f"{body}\nComment: {comments_text}" if body else f"Comment: {comments_text}"
    else:
        full_text = body

    metadata: dict[str, str | list[str]] = {
        "object_type": "Issue",
        "number": str(issue.number),
        "state": issue.state,
    }
    if author := _safe_user_login(issue.user):
        metadata["author"] = author
    if closer := _safe_user_login(issue.closed_by):
        metadata["closed_by"] = closer
    if assignees := [
        login for login in (_safe_user_login(a) for a in issue.assignees) if login
    ]:
        metadata["assignees"] = assignees
    if labels := [label.name for label in issue.labels]:
        metadata["labels"] = labels
    if issue.repository:
        metadata["repo"] = issue.repository.full_name
    if created := _utc(issue.created_at):
        metadata["created_at"] = created
    if closed := _utc(issue.closed_at):
        metadata["closed_at"] = closed

    primary_owner = _basic_expert_from_user(issue.user)

    return Document(
        id=issue.html_url,
        sections=[Section(link=issue.html_url, text=full_text)],
        source=DocumentSource.GITHUB,
        semantic_identifier=f"{issue.number}: {issue.title}",
        # updated_at is UTC time but is timezone unaware
        doc_updated_at=issue.updated_at.replace(tzinfo=timezone.utc),
        primary_owners=[primary_owner] if primary_owner else None,
        metadata=metadata,
    )


class GithubConnector(LoadConnector, PollConnector):
    def __init__(
        self,
        repo_owner: str,
        repo_name: str = "",
        batch_size: int = INDEX_BATCH_SIZE,
        state_filter: str = "all",
        include_prs: bool = True,
        include_issues: bool = False,
    ) -> None:
        self.repo_owner = repo_owner
        # repo_name semantics:
        #   ""           -> index every repo the owner has access to
        #   "foo"        -> single repo (legacy shape, unchanged)
        #   "foo,bar,..."-> comma-separated list of repo names
        self.repo_name = repo_name or ""
        self.batch_size = batch_size
        self.state_filter = state_filter
        self.include_prs = include_prs
        self.include_issues = include_issues
        self.github_client: Github | None = None

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        token = credentials["github_access_token"]
        self.github_client = (
            Github(token, base_url=GITHUB_CONNECTOR_BASE_URL, per_page=_ITEMS_PER_PAGE)
            if GITHUB_CONNECTOR_BASE_URL
            else Github(token, per_page=_ITEMS_PER_PAGE)
        )
        return None

    def _get_github_repo(
        self, github_client: Github, full_name: str, attempt_num: int = 0
    ) -> Repository.Repository:
        if attempt_num > _MAX_NUM_RATE_LIMIT_RETRIES:
            raise RuntimeError(
                "Re-tried fetching repo too many times. Something is going wrong with fetching objects from Github"
            )

        try:
            return github_client.get_repo(full_name)
        except RateLimitExceededException:
            _sleep_after_rate_limit_exception(github_client)
            return self._get_github_repo(github_client, full_name, attempt_num + 1)

    def _get_all_repos(
        self, github_client: Github, attempt_num: int = 0
    ) -> list[Repository.Repository]:
        if attempt_num > _MAX_NUM_RATE_LIMIT_RETRIES:
            raise RuntimeError(
                "Re-tried fetching repos too many times. Something is going wrong with fetching objects from Github"
            )
        try:
            try:
                org = github_client.get_organization(self.repo_owner)
                return list(org.get_repos())
            except GithubException:
                user = github_client.get_user(self.repo_owner)
                return list(user.get_repos())
        except RateLimitExceededException:
            _sleep_after_rate_limit_exception(github_client)
            return self._get_all_repos(github_client, attempt_num + 1)

    def _resolve_repos(self) -> list[Repository.Repository]:
        if self.github_client is None:
            raise ConnectorMissingCredentialError("GitHub")

        if not self.repo_name:
            return self._get_all_repos(self.github_client)

        names = [n.strip() for n in self.repo_name.split(",") if n.strip()]
        repos: list[Repository.Repository] = []
        for name in names:
            try:
                repos.append(
                    self._get_github_repo(
                        self.github_client, f"{self.repo_owner}/{name}"
                    )
                )
            except GithubException as e:
                logger.warning(f"Could not fetch repo {self.repo_owner}/{name}: {e}")
        return repos

    def _fetch_from_github(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> GenerateDocumentsOutput:
        if self.github_client is None:
            raise ConnectorMissingCredentialError("GitHub")

        for repo in self._resolve_repos():
            logger.info(f"Indexing repo: {repo.full_name}")

            if self.include_prs:
                pull_requests = repo.get_pulls(
                    state=self.state_filter, sort="updated", direction="desc"
                )

                stop_outer = False
                for pr_batch in _batch_github_objects(
                    pull_requests, self.github_client, self.batch_size
                ):
                    doc_batch: list[Document] = []
                    for pr in pr_batch:
                        if start is not None and pr.updated_at < start:
                            stop_outer = True
                            break
                        if end is not None and pr.updated_at > end:
                            continue
                        doc_batch.append(_convert_pr_to_document(cast(PullRequest, pr)))
                    if doc_batch:
                        yield doc_batch
                    if stop_outer:
                        break

            if self.include_issues:
                issues = repo.get_issues(
                    state=self.state_filter, sort="updated", direction="desc"
                )

                stop_outer = False
                for issue_batch in _batch_github_objects(
                    issues, self.github_client, self.batch_size
                ):
                    doc_batch = []
                    for issue in issue_batch:
                        issue = cast(Issue, issue)
                        if start is not None and issue.updated_at < start:
                            stop_outer = True
                            break
                        if end is not None and issue.updated_at > end:
                            continue
                        if issue.pull_request is not None:
                            # PRs are handled separately
                            continue
                        doc_batch.append(_convert_issue_to_document(issue))
                    if doc_batch:
                        yield doc_batch
                    if stop_outer:
                        break

    def load_from_state(self) -> GenerateDocumentsOutput:
        return self._fetch_from_github()

    def poll_source(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch
    ) -> GenerateDocumentsOutput:
        start_datetime = datetime.fromtimestamp(start, tz=timezone.utc).replace(
            tzinfo=None
        )
        end_datetime = datetime.fromtimestamp(end, tz=timezone.utc).replace(
            tzinfo=None
        )

        # Move start time back by 3 hours, since some Issues/PRs are getting dropped
        # Could be due to delayed processing on GitHub side
        # The non-updated issues since last poll will be shortcut-ed and not embedded
        adjusted_start_datetime = start_datetime - timedelta(hours=3)

        epoch = datetime(1970, 1, 1)
        if adjusted_start_datetime < epoch:
            adjusted_start_datetime = epoch

        return self._fetch_from_github(adjusted_start_datetime, end_datetime)


if __name__ == "__main__":
    import os

    connector = GithubConnector(
        repo_owner=os.environ["REPO_OWNER"],
        repo_name=os.environ.get("REPO_NAME", ""),
    )
    connector.load_credentials(
        {"github_access_token": os.environ["GITHUB_ACCESS_TOKEN"]}
    )
    document_batches = connector.load_from_state()
    print(next(document_batches))
