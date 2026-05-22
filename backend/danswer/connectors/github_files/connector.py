"""GitHub Files connector — indexes files matching a given extension under
a configurable path prefix in a repository.

Two modes:

- Fixed-depth (default): matches `<path_prefix>/<single_dir>/<file><extension>`
  — i.e. exactly one folder under the prefix, file directly inside. Default
  settings target a service-catalog layout:
      service-catalog/products/<product>/<file>.json
  Anything deeper or shallower is skipped.

- Recursive (`recursive=True`): walks every folder under `path_prefix`
  (the whole repo if the prefix is empty) and matches by extension at any
  depth. Useful for "index all .md files in the repo" style configurations.

The connector reuses the existing GitHub access token credential shape, so
users don't need to re-enter their PAT.
"""
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from github import Github
from github import GithubException
from github import RateLimitExceededException

from danswer.configs.app_configs import GITHUB_CONNECTOR_BASE_URL
from danswer.configs.app_configs import INDEX_BATCH_SIZE
from danswer.configs.constants import DocumentSource
from danswer.connectors.interfaces import GenerateDocumentsOutput
from danswer.connectors.interfaces import LoadConnector
from danswer.connectors.interfaces import PollConnector
from danswer.connectors.interfaces import SecondsSinceUnixEpoch
from danswer.connectors.models import ConnectorMissingCredentialError
from danswer.connectors.models import Document
from danswer.connectors.models import Section
from danswer.utils.logger import setup_logger


logger = setup_logger()


_MAX_NUM_RATE_LIMIT_RETRIES = 5
_ITEMS_PER_PAGE = 100
_DEFAULT_PATH_PREFIX = "service-catalog/products"
_DEFAULT_FILE_EXTENSION = ".json"


def _sleep_after_rate_limit_exception(github_client: Github) -> None:
    sleep_time = github_client.get_rate_limit().core.reset.replace(
        tzinfo=timezone.utc
    ) - datetime.now(tz=timezone.utc)
    sleep_time += timedelta(minutes=1)
    sleep_seconds = max(0, int(sleep_time.total_seconds()))
    logger.info(f"Hit GitHub rate-limit. Sleeping {sleep_seconds}s.")
    time.sleep(sleep_seconds)


def _retry_on_rate_limit(github_client: Github, fn, *args, **kwargs):
    """Run `fn(*args, **kwargs)` retrying on RateLimitExceededException."""
    for attempt in range(_MAX_NUM_RATE_LIMIT_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except RateLimitExceededException:
            if attempt >= _MAX_NUM_RATE_LIMIT_RETRIES:
                raise
            _sleep_after_rate_limit_exception(github_client)
    raise RuntimeError("unreachable")


class GithubFilesConnector(LoadConnector, PollConnector):
    def __init__(
        self,
        repo_owner: str,
        repo_name: str,
        path_prefix: str = _DEFAULT_PATH_PREFIX,
        file_extension: str = _DEFAULT_FILE_EXTENSION,
        branch: str = "",
        recursive: bool = False,
        batch_size: int = INDEX_BATCH_SIZE,
    ) -> None:
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.path_prefix = path_prefix.strip("/")
        # Normalize: caller can pass "json" or ".json"
        self.file_extension = (
            file_extension if file_extension.startswith(".") else f".{file_extension}"
        ).lower()
        self.branch = branch or ""  # empty -> use repo's default branch
        self.recursive = recursive
        self.batch_size = batch_size
        self.github_client: Github | None = None

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        token = credentials["github_access_token"]
        self.github_client = (
            Github(token, base_url=GITHUB_CONNECTOR_BASE_URL, per_page=_ITEMS_PER_PAGE)
            if GITHUB_CONNECTOR_BASE_URL
            else Github(token, per_page=_ITEMS_PER_PAGE)
        )
        return None

    def _open_repo(self):
        if self.github_client is None:
            raise ConnectorMissingCredentialError("GitHub")
        return _retry_on_rate_limit(
            self.github_client,
            self.github_client.get_repo,
            f"{self.repo_owner}/{self.repo_name}",
        )

    def _resolve_branch(self, repo) -> str:
        return self.branch or repo.default_branch

    def _list_matching_paths(self, repo, branch: str) -> list[tuple[str, str]]:
        """Walk the git tree once, returning (path, blob_sha) pairs for files
        matching the configured extension. In fixed-depth mode, only files at
        `<prefix>/<single_dir>/<file><extension>` match; in recursive mode,
        any file under `<prefix>` (or the repo root) at any depth matches."""
        branch_obj = _retry_on_rate_limit(self.github_client, repo.get_branch, branch)
        head_sha = branch_obj.commit.sha
        tree = _retry_on_rate_limit(
            self.github_client, repo.get_git_tree, head_sha, True
        )

        prefix = self.path_prefix
        expected_depth = len(prefix.split("/")) + 2 if prefix else 2

        results: list[tuple[str, str]] = []
        for element in tree.tree:
            if element.type != "blob":
                continue
            path = element.path
            if prefix and not path.startswith(prefix + "/"):
                continue
            if not self.recursive:
                parts = path.split("/")
                if len(parts) != expected_depth:
                    continue
            if not path.lower().endswith(self.file_extension):
                continue
            results.append((path, element.sha))

        if tree.raw_data.get("truncated"):
            logger.warning(
                "Git tree was truncated by GitHub. Some files in the configured "
                "path may have been missed. Consider narrowing the prefix or "
                "indexing the repo via the standard GitHub connector instead."
            )

        return results

    def _fetch_blob_text(self, repo, path: str, branch: str) -> str | None:
        try:
            content = _retry_on_rate_limit(
                self.github_client, repo.get_contents, path, branch
            )
        except GithubException as e:
            logger.warning(f"Failed to fetch {path}: {e}")
            return None
        try:
            return content.decoded_content.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning(f"Skipping non-UTF8 file: {path}")
            return None

    def _convert_to_document(
        self,
        repo,
        path: str,
        sha: str,
        text: str,
        branch: str,
        doc_updated_at: datetime,
    ) -> Document:
        parts = path.split("/")
        product_dir = parts[-2] if len(parts) >= 2 else ""
        filename = parts[-1]
        html_url = f"https://github.com/{repo.full_name}/blob/{branch}/{path}"
        # Use the blob SHA in the document id so a content change always
        # produces a fresh id and the indexer treats it as a new version,
        # while unchanged files stay deduped across runs.
        doc_id = f"{html_url}@{sha}"

        # In recursive mode files at different depths can share a filename, so
        # use the full repo-relative path as the semantic identifier.
        if self.recursive:
            semantic_identifier = path
        else:
            semantic_identifier = (
                f"{product_dir}/{filename}" if product_dir else filename
            )

        return Document(
            id=doc_id,
            sections=[Section(link=html_url, text=text)],
            source=DocumentSource.GITHUB_FILES,
            semantic_identifier=semantic_identifier,
            doc_updated_at=doc_updated_at,
            metadata={
                "repo": repo.full_name,
                "path": path,
                "product": product_dir,
                "branch": branch,
                "blob_sha": sha,
            },
        )

    def _fetch_documents(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> GenerateDocumentsOutput:
        if self.github_client is None:
            raise ConnectorMissingCredentialError("GitHub")

        repo = self._open_repo()
        branch = self._resolve_branch(repo)

        # If polling, check whether any commit on this branch (within the
        # configured prefix) landed in the window before doing real work.
        if start is not None or end is not None:
            try:
                commits_iter = repo.get_commits(
                    sha=branch,
                    **({"path": self.path_prefix} if self.path_prefix else {}),
                    **({"since": start} if start else {}),
                    **({"until": end} if end else {}),
                )
                # Materializing one element triggers the API call without
                # paging through everything.
                first = next(iter(commits_iter), None)
                if first is None:
                    logger.info(
                        f"No commits affecting '{self.path_prefix}' on branch "
                        f"'{branch}' between {start} and {end}; skipping fetch."
                    )
                    return
            except GithubException as e:
                # Don't fail the run if the commit check 422's (e.g. empty path);
                # fall through and just re-fetch.
                logger.warning(
                    f"Commit-window check failed ({e}); re-fetching all matching files."
                )

        matches = self._list_matching_paths(repo, branch)
        logger.info(
            f"GitHub-Files: found {len(matches)} matching file(s) under "
            f"'{self.path_prefix}' on {repo.full_name}@{branch}."
        )

        # Use the most recent commit on the branch as doc_updated_at — gives
        # a real timestamp without per-file commit lookups.
        try:
            head_commit = _retry_on_rate_limit(
                self.github_client, repo.get_branch, branch
            ).commit
            commit_dt = head_commit.commit.author.date
            if commit_dt.tzinfo is None:
                commit_dt = commit_dt.replace(tzinfo=timezone.utc)
        except Exception:
            commit_dt = datetime.now(tz=timezone.utc)

        doc_batch: list[Document] = []
        for path, sha in matches:
            text = self._fetch_blob_text(repo, path, branch)
            if text is None:
                continue
            doc_batch.append(
                self._convert_to_document(repo, path, sha, text, branch, commit_dt)
            )
            if len(doc_batch) >= self.batch_size:
                yield doc_batch
                doc_batch = []
        if doc_batch:
            yield doc_batch

    def load_from_state(self) -> GenerateDocumentsOutput:
        return self._fetch_documents()

    def poll_source(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch
    ) -> GenerateDocumentsOutput:
        start_dt = datetime.fromtimestamp(start, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(end, tz=timezone.utc)
        return self._fetch_documents(start=start_dt, end=end_dt)


if __name__ == "__main__":
    import os

    connector = GithubFilesConnector(
        repo_owner=os.environ["REPO_OWNER"],
        repo_name=os.environ["REPO_NAME"],
        path_prefix=os.environ.get("PATH_PREFIX", _DEFAULT_PATH_PREFIX),
        file_extension=os.environ.get("FILE_EXTENSION", _DEFAULT_FILE_EXTENSION),
        branch=os.environ.get("BRANCH", ""),
        recursive=os.environ.get("RECURSIVE", "").lower() in ("1", "true", "yes"),
    )
    connector.load_credentials(
        {"github_access_token": os.environ["GITHUB_ACCESS_TOKEN"]}
    )
    print(next(connector.load_from_state()))
