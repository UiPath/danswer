#!/usr/bin/env bash
#
# guarded-apply.sh <overlay> [namespace]
#
# Wrapper around `kubectl apply -k k8s/overlays/<overlay>` that refuses to
# apply a Vespa version pin which would jump too far from the version the
# cluster is ACTUALLY running.
#
# Why this exists: Vespa's config server refuses an auto-upgrade spanning
# more than MAX_UPGRADE_HOP minor releases (see AGENTS.md "Critical facts
# §10"). A bare tag bump that crosses that gap crash-loops the config
# server and takes the whole cluster down. This guard catches it BEFORE
# the apply reaches the cluster.
#
# It checks against the LIVE running version (not the repo's previous pin)
# on purpose — config can drift out of git, so live is the only truth that
# matters at apply time.
#
# Usage:
#   k8s/scripts/guarded-apply.sh prod
#   k8s/scripts/guarded-apply.sh local default
#   FORCE=1 k8s/scripts/guarded-apply.sh prod   # override the guard (you accept the risk)
#
set -euo pipefail

OVERLAY="${1:?usage: guarded-apply.sh <prod|local> [namespace]}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/overlays/${OVERLAY}"
# Default namespace per overlay (prod → darwin, else → default).
if [ "${2:-}" != "" ]; then NS="$2"; elif [ "$OVERLAY" = "prod" ]; then NS="darwin"; else NS="default"; fi
MAX_UPGRADE_HOP=30   # Vespa's documented limit (minor releases per upgrade)

[ -d "$DIR" ] || { echo "ERROR: overlay dir not found: $DIR"; exit 2; }

minor() { echo "$1" | cut -d. -f2; }
major() { echo "$1" | cut -d. -f1; }

# New Vespa version this overlay would deploy (from the rendered manifests).
new_ver=$(kubectl kustomize "$DIR" 2>/dev/null \
  | grep -oE 'image: *vespaengine/vespa:[0-9]+\.[0-9]+\.[0-9]+' | head -1 | sed -E 's/.*://')

# Current running version: the image tag on a live Vespa StatefulSet.
cur_ver=$(kubectl get statefulset vespa-content -n "$NS" \
  -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null \
  | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)

echo "Vespa version guard: current(live)=${cur_ver:-<unknown>}  new(overlay)=${new_ver:-<unparseable>}  ns=$NS"

abort() { echo "REFUSING TO APPLY. $1"; echo "Override with FORCE=1 if you understand the risk."; [ "${FORCE:-0}" = "1" ] && { echo "FORCE=1 set — proceeding anyway."; return 0; }; exit 1; }

if [ -z "$new_ver" ]; then
  abort "Could not parse a pinned vespaengine/vespa:X.Y.Z from the overlay — is it on a floating tag (:latest)? Pin an explicit version."
elif [ -z "$cur_ver" ]; then
  echo "WARNING: could not read the live Vespa version (no cluster access, or vespa-content not deployed yet). Skipping the gap check — verify manually."
elif [ "$(major "$cur_ver")" != "$(major "$new_ver")" ]; then
  abort "Vespa MAJOR version change $cur_ver -> $new_ver. Major upgrades need a dedicated migration, not this guard. "
else
  gap=$(( $(minor "$new_ver") - $(minor "$cur_ver") ))
  if [ "$gap" -gt "$MAX_UPGRADE_HOP" ]; then
    abort "Vespa UPGRADE $cur_ver -> $new_ver is $gap minor releases (> $MAX_UPGRADE_HOP). Vespa will refuse this and crash-loop the config server. Do a STEPWISE upgrade (<=$MAX_UPGRADE_HOP per hop). "
  elif [ "$gap" -lt "-$MAX_UPGRADE_HOP" ]; then
    echo "WARNING: large DOWNGRADE $cur_ver -> $new_ver (${gap} minors). This is OK only if $new_ver matches the on-disk index format (e.g. recovering after an accidental upgrade). If unsure, STOP."
    [ "${FORCE:-0}" = "1" ] || { echo "Re-run with FORCE=1 to confirm the downgrade."; exit 1; }
  else
    echo "OK: Vespa $cur_ver -> $new_ver is within the $MAX_UPGRADE_HOP-release limit."
  fi
fi

echo "--- kubectl diff (review before apply) ---"
kubectl diff -k "$DIR" || true   # diff exits non-zero when there ARE differences; that's expected
echo "--- applying ---"
kubectl apply -k "$DIR"
