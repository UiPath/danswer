"""List Account fields available on the connected Salesforce org.

Authenticates with the OAuth Username-Password flow, calls the Account
`describe` endpoint, and prints every field's API name plus its display
label. Useful when curating ACCOUNT_FIELDS in
`danswer/connectors/salesforce/connector.py` — run this against your org
and cross-reference the output with the labels you want to index.

Usage:

    SF_CLIENT_ID=... SF_CLIENT_SECRET=... \\
    SF_USERNAME=... SF_PASSWORD=... \\
    [SF_LOGIN_URL=https://test.salesforce.com] \\
    [SF_FILTER=label_or_api_substring] \\
    [SF_ONLY_CUSTOM=1] \\
    python backend/scripts/list_salesforce_account_fields.py
"""

import os
import sys

import requests

DEFAULT_LOGIN_URL = "https://login.salesforce.com"
SF_API_VERSION = "v59.0"


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
            f"  response: {err}\n"
            "Common causes:\n"
            "  * invalid_client_id / invalid_client: wrong sf_client_id or sf_client_secret\n"
            "  * invalid_grant + 'authentication failure': wrong username/password,\n"
            "    or password needs the security token appended (password+token)\n"
            "  * invalid_grant + 'user hasn't approved this consumer': Connected App\n"
            "    'Permitted Users' must be 'All users may self-authorize'\n"
            "  * sandbox accounts: set SF_LOGIN_URL=https://test.salesforce.com\n"
        )
    body = resp.json()
    return body["access_token"], body["instance_url"]


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
    only_custom = os.environ.get("SF_ONLY_CUSTOM") == "1"
    needle = os.environ.get("SF_FILTER", "").lower()

    access_token, instance_url = _get_access_token(
        client_id, client_secret, username, password, login_url
    )

    desc = requests.get(
        f"{instance_url}/services/data/{SF_API_VERSION}/sobjects/Account/describe",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=60,
    )
    desc.raise_for_status()
    fields = desc.json()["fields"]

    rows: list[tuple[str, str, str, str]] = []
    for field in fields:
        if only_custom and not field.get("custom"):
            continue
        api_name = field.get("name", "")
        label = field.get("label", "")
        ftype = field.get("type", "")
        ref = ",".join(field.get("referenceTo") or [])
        if needle and needle not in api_name.lower() and needle not in label.lower():
            continue
        rows.append((api_name, label, ftype, ref))

    rows.sort(key=lambda r: r[0].lower())

    name_w = max((len(r[0]) for r in rows), default=10)
    label_w = max((len(r[1]) for r in rows), default=10)
    type_w = max((len(r[2]) for r in rows), default=8)

    header = f"{'API Name'.ljust(name_w)}  {'Label'.ljust(label_w)}  {'Type'.ljust(type_w)}  References"
    print(header)
    print("-" * len(header))
    for api_name, label, ftype, ref in rows:
        print(f"{api_name.ljust(name_w)}  {label.ljust(label_w)}  {ftype.ljust(type_w)}  {ref}")

    print(f"\n{len(rows)} field(s){' (custom only)' if only_custom else ''}.")
    if rows and any(r[2] == "reference" for r in rows):
        print(
            "Note: 'reference' fields are lookups — index them as "
            "<API_NAME without __c>__r.Name (or .Id) rather than the raw API name."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
