import os
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Tuple

import requests

from danswer.configs.app_configs import INDEX_BATCH_SIZE
from danswer.configs.constants import DocumentSource
from danswer.connectors.cross_connector_utils.miscellaneous_utils import time_str_to_utc
from danswer.connectors.interfaces import GenerateDocumentsOutput
from danswer.connectors.interfaces import IdConnector
from danswer.connectors.interfaces import LoadConnector
from danswer.connectors.interfaces import PollConnector
from danswer.connectors.interfaces import SecondsSinceUnixEpoch
from danswer.connectors.models import BasicExpertInfo
from danswer.connectors.models import ConnectorMissingCredentialError
from danswer.connectors.models import Document
from danswer.connectors.models import Section
from danswer.connectors.salesforce.utils import extract_dict_text
from danswer.utils.logger import setup_logger

ID_PREFIX = "SALESFORCE_"
DEFAULT_SF_LOGIN_URL = "https://login.salesforce.com"
SF_API_VERSION = "v59.0"

# Account fields to index, as (api_path, friendly_label) pairs.
#   - api_path is what Salesforce sees in the SELECT clause (SOQL doesn't
#     support column aliasing in non-aggregate queries, so renaming has to
#     happen client-side).
#   - friendly_label is what appears as the key in the indexed text. Use
#     dot traversal in the api_path for lookup relationships (e.g. CSM__r.Name)
#     and the leaf value gets hoisted to the top level under the friendly
#     label, flattening the JSON.
#
# To re-verify against your org's actual fields, run
# `backend/scripts/list_salesforce_account_fields.py`.
ACCOUNT_FIELDS: list[tuple[str, str]] = [
    # Identity / naming
    ("Id", "Account Id"),
    ("Name", "Account Name"),
    ("Legal__c", "Legal Entity Name"),
    ("Business_Name__c", "Business Name"),
    # Classification / categorization
    ("RecordType.Name", "Account Record Type"),
    ("Account_Type__c", "Account Type"),
    ("Classification__c", "Classification"),
    ("Segmentation__c", "Segmentation"),
    # Ownership
    ("Owner.Name", "Account Owner"),
    ("Account_Owner_Email__c", "Account Owner Email"),
    # System fields needed for Document construction; their friendly labels
    # are kept ending in Id/Date so danswer's SF_JSON_FILTER strips them
    # from the searchable text body.
    ("LastModifiedDate", "LastModifiedDate"),
    ("LastModifiedBy.Name", "Last Modified By"),
    # Custom fields — verified against this org's describe
    ("Geo__c", "Geo"),
    ("Area__c", "Area"),
    ("Country__c", "Country"),
    ("Ruby_Account__c", "Ruby Account"),
    ("Maintenance_Flag__c", "Maintenance Flag"),
    ("CSM__r.Name", "CSM"),
    ("Support_Technical_Advisor__r.Name", "TAM"),
    ("Vertical1__c", "Vertical"),
    ("Sub_Vertical__c", "Sub-Vertical"),
    ("CSD__r.Name", "CSD"),
    ("NumberOfEmployees", "Employees"),
    ("Annual_Contract_Value__c", "Annual Contract Value"),
    # Requested fields with no match on this org's Account object. If they
    # actually exist under different API names, drop them in here:
    #   BDR                  — no field found
    #   Lead Sales Engineer  — no field found
    #   Territory Domain     — no field found
]


def _resolve_path(record: dict[str, Any], path: str) -> Any:
    """Walk a dotted SOQL path through the response JSON. Returns None if any
    segment is missing or not a dict."""
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _to_friendly_dict(record: dict[str, Any]) -> dict[str, Any]:
    """Re-key a SOQL record using ACCOUNT_FIELDS' friendly labels, flattening
    dot-traversal paths so e.g. CSM__r.Name -> {"CSM": "Joe Smith"}."""
    out: dict[str, Any] = {}
    for api_path, label in ACCOUNT_FIELDS:
        value = _resolve_path(record, api_path)
        if value is not None:
            out[label] = value
    return out


def _soql_datetime(dt: datetime) -> str:
    """Format a datetime for a SOQL date literal.

    Uses the trailing-Z form (`YYYY-MM-DDThh:mm:ss.000Z`) rather than
    `+hh:mm` because the `+` would otherwise survive into the URL query
    string and risk being decoded as a space by intermediate parsers,
    silently turning the WHERE clause into a no-match.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

logger = setup_logger()


class SalesforceConnector(LoadConnector, PollConnector, IdConnector):
    def __init__(
        self,
        batch_size: int = INDEX_BATCH_SIZE,
        requested_objects: list[str] = [],
    ) -> None:
        self.batch_size = batch_size
        # `requested_objects` is accepted for backward compatibility with the
        # old multi-object connector configuration shape (e.g. ["Account"]).
        # The Account-only connector ignores it; filtering is now driven by
        # the env-var filters below. Warn loudly so stale configs don't go
        # unnoticed.
        if requested_objects:
            logger.warning(
                "SalesforceConnector: 'requested_objects' is deprecated and "
                "ignored (got %r). Use SF_ACCOUNT_NAME_FILTER / "
                "SF_MAINTENANCE_FLAG_FILTER env vars to scope the query.",
                requested_objects,
            )
        # Optional WHERE-clause filters from env vars. Each is read once at
        # connector construction; restart the indexer to pick up changes.
        #   SF_ACCOUNT_NAME_FILTER     — substring match (Name LIKE '%value%').
        #                                 Use SF_ACCOUNT_NAME_EXACT=1 for an
        #                                 exact = match instead.
        #   SF_MAINTENANCE_FLAG_FILTER — exact match on the Maintenance_Flag__c
        #                                 picklist. Comma-separated for IN(...).
        self.account_name_filter: str | None = (
            os.environ.get("SF_ACCOUNT_NAME_FILTER", "").strip() or None
        )
        self.account_name_exact: bool = (
            os.environ.get("SF_ACCOUNT_NAME_EXACT", "").strip() == "1"
        )
        maint_raw = os.environ.get("SF_MAINTENANCE_FLAG_FILTER", "").strip()
        self.maintenance_flag_filter: list[str] | None = (
            [v.strip() for v in maint_raw.split(",") if v.strip()]
            if maint_raw
            else None
        )
        self.access_token: str | None = None
        self.instance_url: str | None = None
        self.headers: dict[str, str] = {}

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        self.access_token, self.instance_url = self._get_access_token(
            client_id=credentials["sf_client_id"],
            client_secret=credentials["sf_client_secret"],
            username=credentials["sf_username"],
            password=credentials["sf_password"],
            login_url=credentials.get("sf_login_url", DEFAULT_SF_LOGIN_URL),
        )
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        return None

    def _get_access_token(
        self,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
        login_url: str,
    ) -> Tuple[str, str]:
        response = requests.post(
            f"{login_url.rstrip('/')}/services/oauth2/token",
            data={
                "grant_type": "password",
                "client_id": client_id,
                "client_secret": client_secret,
                "username": username,
                "password": password,
            },
            timeout=30,
        )
        if response.status_code != 200:
            logger.error(f"Salesforce authentication failed: {response.text}")
            raise Exception("Failed to authenticate with Salesforce.")
        data = response.json()
        logger.info("Successfully authenticated with Salesforce.")
        return data["access_token"], data["instance_url"]

    def _build_account_query(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        select_fields: list[str] | None = None,
    ) -> str:
        if select_fields is None:
            select_fields = [api_path for api_path, _ in ACCOUNT_FIELDS]
        select_clause = ", ".join(select_fields)
        clauses: list[str] = []

        if self.account_name_filter:
            safe = self.account_name_filter.replace("'", r"\'")
            if self.account_name_exact:
                clauses.append(f"Name = '{safe}'")
            else:
                clauses.append(f"Name LIKE '%{safe}%'")

        if self.maintenance_flag_filter:
            quoted = ", ".join(
                f"'{v.replace(chr(39), chr(92) + chr(39))}'"
                for v in self.maintenance_flag_filter
            )
            clauses.append(f"Maintenance_Flag__c IN ({quoted})")

        if start is not None:
            clauses.append(f"LastModifiedDate >= {_soql_datetime(start)}")
        if end is not None:
            clauses.append(f"LastModifiedDate <= {_soql_datetime(end)}")

        where_clause = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return f"SELECT {select_clause} FROM Account{where_clause}"

    def _execute_soql(self, query: str) -> list[dict[str, Any]]:
        if self.access_token is None or self.instance_url is None:
            raise ConnectorMissingCredentialError("Salesforce")

        records: list[dict[str, Any]] = []
        url: str | None = f"{self.instance_url}/services/data/{SF_API_VERSION}/query"
        params: dict[str, str] | None = {"q": query}

        while url:
            resp = requests.get(url, headers=self.headers, params=params, timeout=120)
            data = resp.json()
            if resp.status_code != 200 or isinstance(data, list):
                msg = data if isinstance(data, list) else resp.text
                raise Exception(f"Salesforce SOQL error: {msg}")

            records.extend(data.get("records", []))

            next_path = data.get("nextRecordsUrl")
            if data.get("done", True) or not next_path:
                break
            url = f"{self.instance_url}{next_path}"
            params = None  # nextRecordsUrl already encodes the query

        return records

    def _convert_object_instance_to_document(
        self, object_dict: dict[str, Any]
    ) -> Document:
        if self.instance_url is None:
            raise ConnectorMissingCredentialError("Salesforce")

        salesforce_id = object_dict["Id"]
        danswer_salesforce_id = f"{ID_PREFIX}{salesforce_id}"
        extracted_link = f"{self.instance_url}/{salesforce_id}"
        extracted_doc_updated_at = time_str_to_utc(object_dict["LastModifiedDate"])
        extracted_semantic_identifier = object_dict.get("Name", "Unknown Account")
        # Re-key with friendly labels before generating the searchable text.
        extracted_object_text = extract_dict_text(_to_friendly_dict(object_dict))

        owner_name = _resolve_path(object_dict, "LastModifiedBy.Name") or "Unknown"
        extracted_primary_owners = [BasicExpertInfo(display_name=owner_name)]

        return Document(
            id=danswer_salesforce_id,
            sections=[Section(link=extracted_link, text=extracted_object_text)],
            source=DocumentSource.SALESFORCE,
            semantic_identifier=extracted_semantic_identifier,
            doc_updated_at=extracted_doc_updated_at,
            primary_owners=extracted_primary_owners,
            metadata={},
        )

    def _fetch_from_salesforce(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> GenerateDocumentsOutput:
        query = self._build_account_query(start=start, end=end)
        logger.info(f"Running Account SOQL: {query}")
        records = self._execute_soql(query)
        logger.info(f"Number of Account records fetched: {len(records)}")

        doc_batch: list[Document] = []
        for record in records:
            doc_batch.append(self._convert_object_instance_to_document(record))
            if len(doc_batch) >= self.batch_size:
                yield doc_batch
                doc_batch = []
        if doc_batch:
            yield doc_batch

    def load_from_state(self) -> GenerateDocumentsOutput:
        return self._fetch_from_salesforce()

    def poll_source(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch
    ) -> GenerateDocumentsOutput:
        start_datetime = datetime.fromtimestamp(start, tz=timezone.utc)
        end_datetime = datetime.fromtimestamp(end, tz=timezone.utc)
        return self._fetch_from_salesforce(start=start_datetime, end=end_datetime)

    def retrieve_all_source_ids(self) -> set[str]:
        query = self._build_account_query(select_fields=["Id"])
        records = self._execute_soql(query)
        return {f"{ID_PREFIX}{r.get('Id', '')}" for r in records if r.get("Id")}


if __name__ == "__main__":
    connector = SalesforceConnector(
        requested_objects=os.environ.get("REQUESTED_OBJECTS", "").split(",")
        if os.environ.get("REQUESTED_OBJECTS")
        else []
    )

    connector.load_credentials(
        {
            "sf_client_id": os.environ["SF_CLIENT_ID"],
            "sf_client_secret": os.environ["SF_CLIENT_SECRET"],
            "sf_username": os.environ["SF_USERNAME"],
            "sf_password": os.environ["SF_PASSWORD"],
            "sf_login_url": os.environ.get("SF_LOGIN_URL", DEFAULT_SF_LOGIN_URL),
        }
    )
    document_batches = connector.load_from_state()
    print(next(document_batches))
