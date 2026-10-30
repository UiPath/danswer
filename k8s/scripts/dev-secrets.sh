#!/usr/bin/env bash
#
# dev-secrets.sh <export|k8s>
#
# Local-dev secrets come from the DEV Key Vault (darwin-dev-kv), read with
# your own `az login`. Prod secrets live in darwin-prod-kv, which is
# private-endpoint only and never reachable from a laptop.
#
#   export   print `export NAME='value'` lines for the host-run stack
#            (dapi / dbe / dsl). Nothing is written to disk:
#                eval "$(k8s/scripts/dev-secrets.sh export)"
#   k8s      render k8s/overlays/local/secrets.env (mode 600, gitignored) for
#            the local Kubernetes overlay, then `kubectl apply -k k8s/overlays/local`.
#
# Vault secret names use '-' (Key Vault disallows '_'); they map back to env
# var names by '-' -> '_' (GEN-AI-CLIENT-SECRET -> GEN_AI_CLIENT_SECRET).
# To add one:  az keyvault secret set --vault-name darwin-dev-kv -n MY-NAME --value ...
#
# Override the vault with DEV_KEYVAULT=<name>.
set -euo pipefail

VAULT="${DEV_KEYVAULT:-darwin-dev-kv}"
MODE="${1:-}"
case "$MODE" in
  export|k8s) ;;
  *) echo "usage: $0 <export|k8s>" >&2; exit 2 ;;
esac

# One access token for the whole run; it is fed to curl on stdin so it never
# shows up in `ps`.
TOKEN="$(az account get-access-token --resource https://vault.azure.net --query accessToken -o tsv)" || {
  echo "dev-secrets: 'az login' first (needs access to $VAULT)" >&2; exit 1; }
BASE="https://${VAULT}.vault.azure.net"

kv_get() {  # kv_get <path> -> JSON body
  printf 'Authorization: Bearer %s\n' "$TOKEN" |
    curl -fsS -H @- "${BASE}$1?api-version=7.4"
}

names="$(kv_get /secrets | python3 -I -c '
import json, sys
for s in json.load(sys.stdin)["value"]:
    if s["attributes"].get("enabled", True):
        print(s["id"].rsplit("/", 1)[1])
')"

render() {  # render <format>: one line per secret
  local fmt="$1" n env val
  for n in $names; do
    env="${n//-/_}"
    val="$(kv_get "/secrets/$n" | python3 -I -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["value"])')"
    if [ "$fmt" = export ]; then
      # single-quote for the shell; embedded ' becomes '\''
      printf "export %s='%s'\n" "$env" "${val//\'/\'\\\'\'}"
    else
      printf '%s=%s\n' "$env" "$val"
    fi
  done
}

if [ "$MODE" = export ]; then
  render export
else
  out="$(cd "$(dirname "$0")/../overlays/local" && pwd)/secrets.env"
  umask 077
  tmp="$(mktemp "${out}.XXXXXX")"
  trap 'rm -f "$tmp"' EXIT
  render env >"$tmp"
  mv "$tmp" "$out"
  trap - EXIT
  echo "wrote $out ($(wc -l <"$out" | tr -d ' ') secrets from $VAULT)" >&2
fi
