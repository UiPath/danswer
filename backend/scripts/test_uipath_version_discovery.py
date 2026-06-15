"""Ad-hoc check of get_uipath_docs_version_base_urls against the agreed test set.
Run: (venv active, cwd=backend)  python scripts/test_uipath_version_discovery.py
Hits live docs.uipath.com pages."""
from danswer.connectors.web.connector import get_uipath_docs_version_base_urls as expand

TESTS = [
    ("https://docs.uipath.com/robot/standalone/latest", 3, "versioned standalone"),
    ("https://docs.uipath.com/apps/automation-suite/", 3, "versioned on-prem"),
    ("https://docs.uipath.com/activities/other/latest", 1, "evergreen (only latest)"),
    (
        "https://docs.uipath.com/automation-cloud/automation-cloud/latest/admin-guide/using-the-migration-tool",
        1,
        "cloud/evergreen deep page",
    ),
    (
        "https://learn.microsoft.com/en-us/azure/devops/pipelines/yaml-schema/jobs-deployment?view=azure-pipelines",
        1,
        "non-docs.uipath (domain-gated out)",
    ),
]

ok = True
for url, expected_count, label in TESTS:
    res = expand(url, max_versions=3)
    status = "PASS" if len(res) == expected_count else "FAIL"
    if status == "FAIL":
        ok = False
    print(f"\n[{status}] {label}")
    print(f"  in : {url}")
    print(f"  exp: {expected_count} url(s)  got: {len(res)}")
    for u in res:
        print(f"     -> {u}")

print("\n==== ALL PASS ====" if ok else "\n==== FAILURES PRESENT ====")
