"""Preview what the SalesforceConnector would actually index.

Authenticates with the OAuth Username-Password flow, runs the same SOQL
query the connector would build (using ACCOUNT_FIELDS), and prints each
record with the friendly labels — exactly as it would land in the indexed
text body. Use this to sanity-check field values before kicking off a real
indexing run.

Usage:

    SF_CLIENT_ID=... SF_CLIENT_SECRET=... \\
    SF_USERNAME=... SF_PASSWORD='...' \\
    [SF_LOGIN_URL=https://test.salesforce.com] \\
    [SF_LIMIT=5]                          # default 5; set to 0 for unlimited
    [SF_NAME_LIKE=acme]                   # filter Account.Name LIKE '%acme%'
    [SF_ACCOUNT_ID=0011x...]              # fetch one specific Account by Id
    [SF_MAINTENANCE_FLAG_FILTER=Active]   # exact-match Maintenance_Flag__c IN(...);
                                          # comma-separate for multiple values
    [SF_FORMAT=text|json]                 # default text
    python backend/scripts/preview_salesforce_accounts.py
"""

import json
import os
import sys
from typing import Any

import requests

DEFAULT_LOGIN_URL = "https://login.salesforce.com"
SF_API_VERSION = "v59.0"

# Account fields to fetch as (api_path, friendly_label) pairs.
#
# Kept in sync manually with
# `backend/danswer/connectors/salesforce/connector.py::ACCOUNT_FIELDS`.
# This script is intentionally standalone (no danswer imports) so it can
# be run from anywhere with just `requests` installed; if the connector's
# field list changes, mirror it here.
ACCOUNT_FIELDS: list[tuple[str, str]] = [
    ("Id", "Account Id"),
    ("Name", "Account Name"),
    ("Legal__c", "Legal Entity Name"),
    ("Business_Name__c", "Business Name"),
    ("RecordType.Name", "Account Record Type"),
    ("Account_Type__c", "Account Type"),
    ("Classification__c", "Classification"),
    ("Segmentation__c", "Segmentation"),
    ("Owner.Name", "Account Owner"),
    ("Account_Owner_Email__c", "Account Owner Email"),
    ("LastModifiedDate", "LastModifiedDate"),
    ("LastModifiedBy.Name", "Last Modified By"),
    ("Geo__c", "Geo"),
    ("Area__c", "Area"),
    ("Country__c", "Country"),
    ("BillingCity", "Billing City"),
    ("BillingCountry", "Billing Country"),
    ("Ruby_Account__c", "Ruby Account"),
    ("Maintenance_Flag__c", "Maintenance Flag"),
    ("CSM__r.Name", "CSM"),
    ("Support_Technical_Advisor__r.Name", "TAM"),
    ("Vertical1__c", "Vertical"),
    ("Sub_Vertical__c", "Sub-Vertical"),
    ("CSD__r.Name", "CSD"),
    ("NumberOfEmployees", "Employees"),
    ("Annual_Contract_Value__c", "Annual Contract Value"),
    ("Annual_Revenue_Local_Currency__c", "Annual Revenue (Local Currency)"),
]


def _resolve_path(record: dict[str, Any], path: str) -> Any:
    """Walk a dotted SOQL path through the response JSON. Returns None if
    any segment is missing or not a dict."""
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


def _get_access_token(
    client_id: str,
    client_secret: str,
    username: str,
    password: str,
    login_url: str,
) -> tuple[str, str]:
    resp = requests.post(
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
    if resp.status_code != 200:
        try:
            err = resp.json()
        except ValueError:
            err = resp.text
        raise SystemExit(
            f"Salesforce OAuth failed (HTTP {resp.status_code}) at {login_url}\n"
            f"  response: {err}"
        )
    body = resp.json()
    return body["access_token"], body["instance_url"]


def _build_query() -> str:
    select_clause = ", ".join(api_path for api_path, _ in ACCOUNT_FIELDS)
    clauses: list[str] = []

    maintenance_flag = os.environ.get("SF_MAINTENANCE_FLAG_FILTER", "").strip()
    name_like = os.environ.get("SF_NAME_LIKE", "").strip()
    account_id = os.environ.get("SF_ACCOUNT_ID", "").strip()

    if account_id:
        clauses.append(f"Id = '{account_id}'")
    if name_like:
        # escape single quotes inside the LIKE pattern
        safe = name_like.replace("'", r"\'")
        clauses.append(f"Name LIKE '%{safe}%'")
    if maintenance_flag:
        values = [v.strip() for v in maintenance_flag.split(",") if v.strip()]
        if values:
            quoted = ", ".join(f"'{v.replace(chr(39), chr(92) + chr(39))}'" for v in values)
            clauses.append(f"Maintenance_Flag__c IN ({quoted})")

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"SELECT {select_clause} FROM Account{where}"

    try:
        limit = int(os.environ.get("SF_LIMIT", "5"))
    except ValueError:
        limit = 5
    if limit > 0:
        query += f" LIMIT {limit}"
    return query


def _execute_soql(
    instance_url: str, access_token: str, query: str
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {access_token}"}
    records: list[dict[str, Any]] = []
    url: str | None = f"{instance_url}/services/data/{SF_API_VERSION}/query"
    params: dict[str, str] | None = {"q": query}

    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=120)
        data = resp.json()
        if resp.status_code != 200 or isinstance(data, list):
            msg = data if isinstance(data, list) else resp.text
            raise SystemExit(f"Salesforce SOQL error: {msg}")
        records.extend(data.get("records", []))
        next_path = data.get("nextRecordsUrl")
        if data.get("done", True) or not next_path:
            break
        url = f"{instance_url}{next_path}"
        params = None
    return records


def _print_text(records: list[dict[str, Any]]) -> None:
    if not records:
        print("(no records)")
        return
    label_w = max(len(label) for _, label in ACCOUNT_FIELDS)
    for i, record in enumerate(records, start=1):
        friendly = _to_friendly_dict(record)
        print(f"--- Record {i} ---")
        for _, label in ACCOUNT_FIELDS:
            value = friendly.get(label, "")
            print(f"  {label.ljust(label_w)}  {value}")
        print()


def _print_json(records: list[dict[str, Any]]) -> None:
    out = [_to_friendly_dict(r) for r in records]
    print(json.dumps(out, indent=2, default=str))


def main() -> int:
    try:
        client_id = os.environ["SF_CLIENT_ID"]
        client_secret = os.environ["SF_CLIENT_SECRET"]
        username = os.environ["SF_USERNAME"]
        password = os.environ["SF_PASSWORD"]
    except KeyError as e:
        print(f"Missing required env var: {e}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 2

    login_url = os.environ.get("SF_LOGIN_URL", DEFAULT_LOGIN_URL)
    output_format = os.environ.get("SF_FORMAT", "text").lower()

    access_token, instance_url = _get_access_token(
        client_id, client_secret, username, password, login_url
    )

    query = _build_query()
    print(f"# SOQL: {query}\n", file=sys.stderr)

    records = _execute_soql(instance_url, access_token, query)
    print(f"# Fetched {len(records)} record(s)\n", file=sys.stderr)

    if output_format == "json":
        _print_json(records)
    else:
        _print_text(records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
