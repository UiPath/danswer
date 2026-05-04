"""Dump every field of one or more Account records as pretty JSON.

Authenticates with the OAuth Username-Password flow and runs
`SELECT FIELDS(ALL) FROM Account [WHERE …] LIMIT N`. Useful when curating
the connector's ACCOUNT_FIELDS list, inspecting custom-field values, or
verifying that a record matches the filter you intend to apply.

`FIELDS(ALL)` has a hard SOQL limit of <= 200 rows per call (Salesforce
constraint, not ours), so this script is intentionally for spot-checking,
not bulk export.

Usage:

    SF_CLIENT_ID=... SF_CLIENT_SECRET=... \\
    SF_USERNAME=... SF_PASSWORD='...' \\
    [SF_LOGIN_URL=https://test.salesforce.com] \\
    [SF_LIMIT=1]                  # default 1; max 200
    [SF_NAME_LIKE=acme]           # optional Name LIKE '%...%'
    [SF_ACCOUNT_ID=0011x...]      # optional single Account by Id
    [SF_KEEP_ATTRIBUTES=1]        # keep Salesforce 'attributes' block (default: stripped)
    python backend/scripts/dump_salesforce_account.py
"""
import json
import os
import sys
from typing import Any

import requests

DEFAULT_LOGIN_URL = "https://login.salesforce.com"
SF_API_VERSION = "v59.0"
FIELDS_ALL_MAX_LIMIT = 200


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
    clauses: list[str] = []

    name_like = os.environ.get("SF_NAME_LIKE", "").strip()
    account_id = os.environ.get("SF_ACCOUNT_ID", "").strip()

    if account_id:
        safe = account_id.replace("'", r"\'")
        clauses.append(f"Id = '{safe}'")
    if name_like:
        safe = name_like.replace("'", r"\'")
        clauses.append(f"Name LIKE '%{safe}%'")

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    try:
        limit = int(os.environ.get("SF_LIMIT", "1"))
    except ValueError:
        limit = 1
    # FIELDS(ALL) requires an explicit LIMIT and Salesforce enforces a 200 cap.
    limit = max(1, min(limit, FIELDS_ALL_MAX_LIMIT))

    return f"SELECT FIELDS(ALL) FROM Account{where} LIMIT {limit}"


def _execute_soql(
    instance_url: str, access_token: str, query: str
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(
        f"{instance_url}/services/data/{SF_API_VERSION}/query",
        headers=headers,
        params={"q": query},
        timeout=120,
    )
    data = resp.json()
    if resp.status_code != 200 or isinstance(data, list):
        msg = data if isinstance(data, list) else resp.text
        raise SystemExit(f"Salesforce SOQL error: {msg}")
    return data.get("records", [])


def _strip_attributes(value: Any) -> Any:
    """Recursively remove Salesforce's noisy 'attributes' blocks."""
    if isinstance(value, dict):
        return {k: _strip_attributes(v) for k, v in value.items() if k != "attributes"}
    if isinstance(value, list):
        return [_strip_attributes(v) for v in value]
    return value


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
    keep_attrs = os.environ.get("SF_KEEP_ATTRIBUTES", "").strip() == "1"

    access_token, instance_url = _get_access_token(
        client_id, client_secret, username, password, login_url
    )

    query = _build_query()
    print(f"# SOQL: {query}\n", file=sys.stderr)

    records = _execute_soql(instance_url, access_token, query)
    print(f"# Fetched {len(records)} record(s)\n", file=sys.stderr)

    payload = records if keep_attrs else _strip_attributes(records)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
