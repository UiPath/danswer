"""Highspot connector — indexes Spots and Items from a Highspot tenant.

Authenticates via HTTP Basic with an API key/secret pair generated from
Highspot's admin console. Supports an optional `spot_names` allowlist;
when empty, all Spots accessible to the credential are indexed.
"""
