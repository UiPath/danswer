#!/usr/bin/env bash
#
# vespa-upgrade.sh <target-version> [namespace]
#
# Performs an ORDERED, HEALTH-GATED, single-hop Vespa upgrade across the five
# StatefulSets. This logic deliberately lives in a script, NOT the manifests:
# kustomize is declarative and cannot sequence a multi-StatefulSet, version-
# stepped, health-gated rollout. A plain `kubectl apply` of a bumped tag rolls
# every role at once with no ordering — exactly what took prod down once.
#
# What it does, in order (Vespa's recommended sequence):
#   1. guard  — refuse major-version / >MAX_HOP / downgrade jumps (as
#               guarded-apply.sh does), checked against the LIVE version.
#   2. config servers   (vespa-configserver)      rollout, gated on readiness
#   3. admin/controller (vespa-admin)             rollout, gated on readiness
#   4. content nodes    (vespa-content)           ONE ORDINAL AT A TIME via
#               updateStrategy.partition stepping, with an explicit health
#               check between each — this is the data tier, so we never let
#               two content nodes be mid-upgrade at once.
#   5. feed containers  (vespa-feed-container)    rollout, gated on readiness
#   6. query containers (vespa-query-container)   rollout, gated on readiness
#
# Health is checked from INSIDE each pod (kubectl exec → localhost), not via
# port-forward, because the cluster runs Istio and external probes hit mTLS.
#
# Single hop only: this refuses jumps Vespa itself refuses (> MAX_HOP minor
# releases). For a larger upgrade, run it repeatedly with intermediate
# versions (e.g. 8.600.35 -> 8.630.x -> 8.660.x -> ...). Each intermediate
# image must exist on the registry and be on-disk-format compatible.
#
# Usage:
#   k8s/scripts/vespa-upgrade.sh 8.620.43            # prod (ns darwin)
#   k8s/scripts/vespa-upgrade.sh 8.620.43 darwin
#   DRY_RUN=1 k8s/scripts/vespa-upgrade.sh 8.620.43  # print actions, change nothing
#   YES=1     k8s/scripts/vespa-upgrade.sh 8.620.43  # skip the confirm prompt
#   FORCE=1   k8s/scripts/vespa-upgrade.sh 8.620.43  # override the version guard
#
# After a successful run, update the per-role vespa newTag values in the
# overlay kustomization so git ≈ live.
set -euo pipefail

TARGET="${1:?usage: vespa-upgrade.sh <target-version X.Y.Z> [namespace]}"
NS="${2:-darwin}"
REGISTRY_IMAGE="vespaengine/vespa"
MAX_HOP=30   # Vespa's documented per-upgrade minor-release limit

DRY_RUN="${DRY_RUN:-0}"
YES="${YES:-0}"
FORCE="${FORCE:-0}"

# Per-role lookups via case (portable to bash 3.2, which macOS still ships —
# no associative arrays). StatefulSet name, container name, and health port.
ROLES_ORDER="configserver admin content feed query"
ss_of() {        case "$1" in
  configserver) echo vespa-configserver ;;  admin) echo vespa-admin ;;
  content)      echo vespa-content ;;        feed)  echo vespa-feed-container ;;
  query)        echo vespa-query-container ;; *) die "unknown role $1" ;; esac; }
container_of() { ss_of "$1"; }   # container name == StatefulSet name for every role
port_of() {      case "$1" in
  configserver) echo 19071 ;;  admin) echo 19092 ;;  content) echo 19092 ;;
  feed)         echo 8080 ;;   query) echo 8080 ;;   *) die "unknown role $1" ;; esac; }

run() { echo "+ $*"; [ "$DRY_RUN" = "1" ] || "$@"; }
die() { echo "ERROR: $*" >&2; exit 1; }
minor() { echo "$1" | cut -d. -f2; }
major() { echo "$1" | cut -d. -f1; }

[[ "$TARGET" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "target must be X.Y.Z, got '$TARGET'"

echo "Context: $(kubectl config current-context)   namespace: $NS"

# --- version guard (against the LIVE content-node image, the source of truth) ---
CUR=$(kubectl get statefulset "$(ss_of content)" -n "$NS" \
  -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null \
  | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)
[ -n "$CUR" ] || die "could not read live Vespa version from $(ss_of content) in ns $NS (cluster access? deployed?)"

echo "Current(live)=$CUR  Target=$TARGET"
if [ "$CUR" = "$TARGET" ]; then echo "Already at $TARGET — nothing to do."; exit 0; fi
[ "$(major "$CUR")" = "$(major "$TARGET")" ] || \
  die "major-version change $CUR -> $TARGET needs a dedicated migration, not this script."
HOP=$(( $(minor "$TARGET") - $(minor "$CUR") ))
if [ "$HOP" -lt 0 ]; then
  [ "$FORCE" = "1" ] || die "DOWNGRADE $CUR -> $TARGET. Only valid for outage recovery to the on-disk format. Re-run with FORCE=1 if you mean it."
  echo "WARNING: downgrade $CUR -> $TARGET (FORCE=1)."
elif [ "$HOP" -gt "$MAX_HOP" ]; then
  [ "$FORCE" = "1" ] || die "$CUR -> $TARGET is $HOP minors (> $MAX_HOP). Vespa will refuse it and crash-loop the config server. Upgrade STEPWISE via intermediate versions. (FORCE=1 to override — not advised.)"
  echo "WARNING: $HOP-minor hop exceeds $MAX_HOP (FORCE=1)."
else
  echo "OK: $HOP-minor hop is within the $MAX_HOP limit."
fi

echo
echo "Plan (ns=$NS): set ${REGISTRY_IMAGE}:${TARGET} on, in order:"
for r in $ROLES_ORDER; do echo "   - $r  ($(ss_of "$r"))"; done
echo
if [ "$DRY_RUN" != "1" ] && [ "$YES" != "1" ]; then
  read -r -p "Proceed against context '$(kubectl config current-context)' / ns '$NS'? [y/N] " ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ] || die "aborted by user."
fi

# Poll /state/v1/health on a specific pod from inside the vespa container.
health_ok() {
  local pod="$1" container="$2" port="$3"
  local code
  code=$(kubectl exec -n "$NS" "$pod" -c "$container" -- \
    sh -c "curl -s -m 5 -o /dev/null -w '%{http_code}' http://localhost:${port}/state/v1/health" 2>/dev/null || echo "000")
  [ "$code" = "200" ]
}

wait_pod_healthy() {
  local pod="$1" container="$2" port="$3" tries=60
  echo "  waiting for $pod to be Ready + health 200 on :$port ..."
  [ "$DRY_RUN" = "1" ] && { echo "  (dry-run: skip wait)"; return 0; }
  kubectl wait --for=condition=ready "pod/$pod" -n "$NS" --timeout=600s
  for ((i=0;i<tries;i++)); do
    if health_ok "$pod" "$container" "$port"; then echo "  $pod healthy."; return 0; fi
    sleep 10
  done
  die "$pod did not report health 200 on :$port in time — STOPPING. Cluster left mid-upgrade; investigate before continuing."
}

upgrade_simple() {  # roles whose StatefulSet rolling update (readiness-gated) is safe as-is
  local r="$1" ss c port
  ss="$(ss_of "$r")"; c="$(container_of "$r")"; port="$(port_of "$r")"
  echo ">>> [$r] $ss -> ${REGISTRY_IMAGE}:${TARGET}"
  run kubectl set image "statefulset/$ss" "$c=${REGISTRY_IMAGE}:${TARGET}" -n "$NS"
  echo "  rolling out (one pod at a time, gated by readiness probe)..."
  [ "$DRY_RUN" = "1" ] || kubectl rollout status "statefulset/$ss" -n "$NS" --timeout=900s
}

upgrade_content() {  # data tier: one ordinal at a time via partition stepping
  local ss c port n
  ss="$(ss_of content)"; c="$(container_of content)"; port="$(port_of content)"
  n=$(kubectl get statefulset "$ss" -n "$NS" -o jsonpath='{.spec.replicas}')
  echo ">>> [content] $ss ($n replicas) -> ${REGISTRY_IMAGE}:${TARGET}, ONE ordinal at a time"
  # Freeze updates (partition above all ordinals), set image, then release
  # ordinals from highest to lowest, verifying health between each.
  run kubectl patch "statefulset/$ss" -n "$NS" --type merge \
    -p "{\"spec\":{\"updateStrategy\":{\"rollingUpdate\":{\"partition\":$n}}}}"
  run kubectl set image "statefulset/$ss" "$c=${REGISTRY_IMAGE}:${TARGET}" -n "$NS"
  for ((ord=n-1; ord>=0; ord--)); do
    echo "  -- releasing content ordinal $ord"
    run kubectl patch "statefulset/$ss" -n "$NS" --type merge \
      -p "{\"spec\":{\"updateStrategy\":{\"rollingUpdate\":{\"partition\":$ord}}}}"
    wait_pod_healthy "${ss}-${ord}" "$c" "$port"
  done
  echo "  content tier fully upgraded."
}

for r in $ROLES_ORDER; do
  if [ "$r" = "content" ]; then upgrade_content; else upgrade_simple "$r"; fi
  # Verify every pod of this role before advancing to the next role.
  if [ "$DRY_RUN" != "1" ]; then
    for pod in $(kubectl get pods -n "$NS" -l "app=$(ss_of "$r")" -o name | sed 's#pod/##'); do
      health_ok "$pod" "$(container_of "$r")" "$(port_of "$r")" \
        || die "$pod unhealthy after upgrade — STOPPING before next role."
    done
  fi
  echo "<<< [$r] done."
  echo
done

echo "Vespa upgrade $CUR -> $TARGET complete across all roles."
echo "NOW: update the per-role vespa newTag values to \"$TARGET\" in"
echo "     k8s/overlays/{prod,local}-vespa/kustomization.yaml so git matches live."
