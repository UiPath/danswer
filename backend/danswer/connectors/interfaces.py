import abc
from collections.abc import Iterator
from typing import Any

from danswer.connectors.models import Document


SecondsSinceUnixEpoch = float

GenerateDocumentsOutput = Iterator[list[Document]]


class BaseConnector(abc.ABC):
    @abc.abstractmethod
    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        raise NotImplementedError

    @staticmethod
    def parse_metadata(metadata: dict[str, Any]) -> list[str]:
        """Parse the metadata for a document/chunk into a string to pass to Generative AI as additional context"""
        custom_parser_req_msg = (
            "Specific metadata parsing required, connector has not implemented it."
        )
        metadata_lines = []
        for metadata_key, metadata_value in metadata.items():
            if isinstance(metadata_value, str):
                metadata_lines.append(f"{metadata_key}: {metadata_value}")
            elif isinstance(metadata_value, list):
                if not all([isinstance(val, str) for val in metadata_value]):
                    raise RuntimeError(custom_parser_req_msg)
                metadata_lines.append(f'{metadata_key}: {", ".join(metadata_value)}')
            else:
                raise RuntimeError(custom_parser_req_msg)
        return metadata_lines


# Large set update or reindex, generally pulling a complete state or from a savestate file
class LoadConnector(BaseConnector):
    @abc.abstractmethod
    def load_from_state(self) -> GenerateDocumentsOutput:
        raise NotImplementedError


# Small set updates by time
class PollConnector(BaseConnector):
    @abc.abstractmethod
    def poll_source(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch
    ) -> GenerateDocumentsOutput:
        raise NotImplementedError


# Like PollConnector, but the connector reports a resumable checkpoint alongside
# each batch. The checkpoint is an OPAQUE JSON string the connector defines and
# interprets (e.g. SharePoint's Graph delta cursor + phase); the shared layer
# only stores/forwards it. run_indexing persists the checkpoint after the batch
# it accompanies is committed, so a run killed mid-crawl resumes from the last
# persisted checkpoint instead of re-enumerating from scratch. Connectors that
# do NOT implement this take the existing (unchanged) code paths.
CheckpointOutput = Iterator[tuple[list[Document], str | None]]


class CheckpointedConnector(BaseConnector):
    @abc.abstractmethod
    def load_from_checkpoint(
        self,
        start: SecondsSinceUnixEpoch,
        end: SecondsSinceUnixEpoch,
        checkpoint: str | None,
    ) -> CheckpointOutput:
        """Yield ``(doc_batch, checkpoint_after_batch)``.

        ``checkpoint`` is the opaque string persisted by a prior run (None on the
        first run). Each yielded ``checkpoint_after_batch`` must be safe to resume
        from ONCE its accompanying ``doc_batch`` has been committed — i.e. calling
        ``load_from_checkpoint`` again with it continues strictly after the docs
        already emitted. Yield ``None`` as the checkpoint for a batch that isn't a
        safe resume point yet.
        """
        raise NotImplementedError


class IdConnector(BaseConnector):
    @abc.abstractmethod
    def retrieve_all_source_ids(self) -> set[str]:
        raise NotImplementedError


# Event driven
class EventConnector(BaseConnector):
    @abc.abstractmethod
    def handle_event(self, event: Any) -> GenerateDocumentsOutput:
        raise NotImplementedError
