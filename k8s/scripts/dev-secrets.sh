#!/usr/bin/env bash
#
# dev-secrets.sh <export|k8s>
#
# Local-dev secrets come from YOUR personal file, ~/.darwin-dev.env (override
# with DARWIN_DEV_ENV=<path>). Every developer keeps their own: local DB
# passwords etc. differ per machine. It is never committed; keep it mode 600.
# Template: k8s/overlays/local/secrets.env.example. Prod secrets are never on
# laptops (Key Vault darwin-prod-kv, private endpoint only).
#
#   export   print `export NAME='value'` lines for the host-run stack
#            (dapi / dbe / dsl):
#                eval "$(k8s/scripts/dev-secrets.sh export)"
#   k8s      copy it to k8s/overlays/local/secrets.env (mode 600, gitignored)
#            for the local Kubernetes overlay, then
#            `kubectl apply -k k8s/overlays/local`.
#
# File format: KEY=VALUE per line (no quotes needed, no `export`); blank lines
# and lines starting with # are ignored.
set -euo pipefail

MODE="${1:-}"
case "$MODE" in
  export|k8s) ;;
  *) echo "usage: $0 <export|k8s>" >&2; exit 2 ;;
esac

SRC="${DARWIN_DEV_ENV:-$HOME/.darwin-dev.env}"
if [ ! -f "$SRC" ]; then
  echo "dev-secrets: $SRC not found — create it from k8s/overlays/local/secrets.env.example (chmod 600)" >&2
  exit 1
fi
if [ -n "$(find "$SRC" -perm -004 2>/dev/null)" ]; then
  echo "dev-secrets: $SRC is world-readable — run: chmod 600 $SRC" >&2
  exit 1
fi

# Emit "NAME<TAB>VALUE" for every valid entry (value is everything after the
# first '='); invalid names are skipped with a warning.
entries() {
  local line name
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    name="${line%%=*}"
    if [[ ! "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || [ "$name" = "$line" ]; then
      echo "dev-secrets: skipping malformed line for '${name:0:40}'" >&2
      continue
    fi
    printf '%s\t%s\n' "$name" "${line#*=}"
  done <"$SRC"
}

if [ "$MODE" = export ]; then
  while IFS=$'\t' read -r name val; do
    # single-quote for the shell; embedded ' becomes '\''
    printf "export %s='%s'\n" "$name" "${val//\'/\'\\\'\'}"
  done < <(entries)
else
  out="$(cd "$(dirname "$0")/../overlays/local" && pwd)/secrets.env"
  umask 077
  tmp="$(mktemp "${out}.XXXXXX")"
  trap 'rm -f "$tmp"' EXIT
  entries | while IFS=$'\t' read -r name val; do printf '%s=%s\n' "$name" "$val"; done >"$tmp"
  mv "$tmp" "$out"
  trap - EXIT
  echo "wrote $out ($(wc -l <"$out" | tr -d ' ') entries from $SRC)" >&2
fi
