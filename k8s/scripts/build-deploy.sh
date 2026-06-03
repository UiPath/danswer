#!/usr/bin/env bash
#
# build-deploy.sh <stage> [component ...]
#
# One command for the backend/web image lifecycle against the Darwin prod
# overlay. Stages are CUMULATIVE — each does everything the lighter stage
# does, then one more thing:
#
#   build    bump tag(s) from kustomization.yaml, docker build (linux/amd64)
#   push     build + docker tag + docker push to the ACR
#   deploy   push  + rewrite kustomization.yaml newTag(s) + kubectl apply -k
#   verify   (standalone) compare LIVE cluster image tags vs the manifest,
#            and report pod health (running / restarts / crashloops)
#
# Components default to BOTH (backend web). Restrict with positional args:
#   build-deploy.sh push backend         # only the backend image
#   build-deploy.sh deploy web           # only the web image
#   build-deploy.sh build                # both
#
# The next tag for each component is computed from the CURRENT newTag in
# k8s/overlays/prod/kustomization.yaml (vha-N -> vha-N+1). The manifest is the
# source of truth and is only EDITED at the `deploy` stage — `build`/`push`
# produce/push the next-tag image without touching the committed manifest, so
# you can build/push first and deploy later (or on another machine).
#
# This is your manual flow, automated:
#   docker build -f ./backend/Dockerfile ./backend -t danswer/danswer-backend:latest --platform linux/amd64
#   docker build -f ./web/Dockerfile     ./web     -t danswer/danswer-web-server:latest --platform=linux/amd64 --load
#   docker tag  danswer/danswer-backend:latest    $REGISTRY/danswer-backend:vha-N
#   docker tag  danswer/danswer-web-server:latest $REGISTRY/danswer-web-server:vha-M
#   docker push $REGISTRY/danswer-backend:vha-N
#   docker push $REGISTRY/danswer-web-server:vha-M
#
# Safety:
#   - `deploy` refuses unless the kubectl context is the prod cluster
#     ($PROD_CONTEXT) — the prod overlay targets it. Override with FORCE=1.
#   - The manifest tag bump is NOT git-committed; the script reminds you.
#   - DRY_RUN=1 prints every docker/kubectl command instead of running it.
#
# Registry auth (push/deploy stages):
#   Credentials are read from the environment — export them in ~/.zshrc:
#       export ACR_USERNAME=<registry username>
#       export ACR_PASSWORD=<registry password / token>
#   The script does `docker login` with them (via --password-stdin, never
#   echoed). If either is unset, push/deploy EXIT immediately (no fallback).
#
# Disk pre-req (build stage):
#   Before building, if the Docker disk is >= DISK_THRESHOLD% (default 80) full,
#   it reclaims space (build cache -> dangling images -> unused images >7d)
#   instead of letting the build fail with "no space left on device".
#   Tune with DISK_THRESHOLD=90; bypass with SKIP_DISK_CHECK=1.
#
set -euo pipefail

# ---- config ---------------------------------------------------------------
REGISTRY="sfbrdevhelmweacr.azurecr.io/danswer"
REGISTRY_HOST="${REGISTRY%%/*}"   # sfbrdevhelmweacr.azurecr.io (login target)
PROD_CONTEXT="darwin"
NAMESPACE="darwin"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KUSTOMIZATION="$REPO_ROOT/k8s/overlays/prod/kustomization.yaml"
OVERLAY_DIR="$REPO_ROOT/k8s/overlays/prod"

# Per-component config. Functions (not associative arrays) so this runs on the
# stock macOS bash 3.2 too — `declare -A` is bash 4+ only.
#   backend: ./backend/Dockerfile  ctx ./backend   local tag danswer/danswer-backend
#   web:     ./web/Dockerfile      ctx ./web       local tag danswer/danswer-web-server
img_logical()      { case "$1" in backend) echo danswer-backend;;     web) echo danswer-web-server;; esac; }
img_local()        { case "$1" in backend) echo danswer/danswer-backend;; web) echo danswer/danswer-web-server;; esac; }
img_dockerfile()   { case "$1" in backend) echo "$REPO_ROOT/backend/Dockerfile";; web) echo "$REPO_ROOT/web/Dockerfile";; esac; }
img_context()      { case "$1" in backend) echo "$REPO_ROOT/backend";; web) echo "$REPO_ROOT/web";; esac; }
# web build adds --load (matches your manual command); backend does not.
img_build_extra()  { case "$1" in web) echo "--load";; *) echo "";; esac; }
# which live deployment to read the running tag from, for `verify`
img_verify_deploy(){ case "$1" in backend) echo api-server-deployment;; web) echo web-server-deployment;; esac; }

# ---- logging --------------------------------------------------------------
log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERR\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [ "${DRY_RUN:-0}" = "1" ]; then printf '\033[2m  $ %s\033[0m\n' "$*"; else "$@"; fi; }

# ---- registry login -------------------------------------------------------
# Credentials come from the environment — export them in ~/.zshrc:
#     export ACR_USERNAME=<registry username>
#     export ACR_PASSWORD=<registry password / token>
# When you run this script from your zsh shell they're already inherited. As a
# fallback (e.g. invoked from a non-zsh context) we pull just those two exports
# out of ~/.zshrc rather than sourcing the whole file (zsh syntax can break
# under bash). Never echoed; piped via --password-stdin.
registry_login() {
  if [ -z "${ACR_USERNAME:-}" ] || [ -z "${ACR_PASSWORD:-}" ]; then
    if [ -f "$HOME/.zshrc" ]; then
      eval "$(grep -E '^[[:space:]]*export[[:space:]]+(ACR_USERNAME|ACR_PASSWORD)=' "$HOME/.zshrc" 2>/dev/null || true)"
    fi
  fi
  if [ -z "${ACR_USERNAME:-}" ] || [ -z "${ACR_PASSWORD:-}" ]; then
    die "ACR_USERNAME/ACR_PASSWORD not set — add them to ~/.zshrc (export ACR_USERNAME=..., export ACR_PASSWORD=...) and retry."
  fi
  log "docker login $REGISTRY_HOST as $ACR_USERNAME"
  if [ "${DRY_RUN:-0}" = "1" ]; then
    printf '\033[2m  $ docker login %s -u %s --password-stdin <<< $ACR_PASSWORD\033[0m\n' "$REGISTRY_HOST" "$ACR_USERNAME"
    return 0
  fi
  printf '%s' "$ACR_PASSWORD" | docker login "$REGISTRY_HOST" -u "$ACR_USERNAME" --password-stdin \
    || die "docker login to $REGISTRY_HOST failed — check ACR_USERNAME/ACR_PASSWORD in ~/.zshrc"
  ok "logged in to $REGISTRY_HOST"
}

# ---- disk pre-req ---------------------------------------------------------
# Before building, make sure there's room — a full Docker disk fails the build
# with "no space left on device" partway through. If usage >= DISK_THRESHOLD%,
# reclaim space with a graduated prune (cheapest/safest first) rather than
# letting the build die. On Docker Desktop (mac) the build runs in a Linux VM;
# DockerRootDir isn't a host path, so we fall back to df of the host root as a
# proxy — pruning the build cache / unused images still frees the VM's disk,
# which is what actually fills up.
disk_used_pct() { df -P "$1" 2>/dev/null | awk 'NR==2{gsub(/%/,"",$5); print $5+0}'; }
ensure_disk_space() {
  [ "${SKIP_DISK_CHECK:-0}" = "1" ] && { warn "SKIP_DISK_CHECK=1 — skipping disk pre-req"; return 0; }
  command -v docker >/dev/null 2>&1 || { warn "docker not found — skipping disk check"; return 0; }
  local threshold="${DISK_THRESHOLD:-80}" root target used
  root="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)"
  target="/"; [ -n "$root" ] && [ -d "$root" ] && target="$root"
  used="$(disk_used_pct "$target")"; used="${used:-0}"
  log "disk pre-req: $target at ${used}% used (threshold ${threshold}%)"
  docker system df 2>/dev/null || true
  [ "$used" -lt "$threshold" ] && { ok "disk ok — no cleanup needed"; return 0; }

  warn "disk >= ${threshold}% — reclaiming Docker space before build"
  run docker builder prune -f || true            # build cache — usually the biggest, fully safe
  run docker image prune -f   || true            # dangling (untagged) images — safe
  used="$(disk_used_pct "$target")"; used="${used:-0}"
  if [ "$used" -ge "$threshold" ]; then
    warn "still ${used}% — pruning unused images older than 7d"
    run docker image prune -af --filter "until=168h" || true   # unused tagged images >7d old
    used="$(disk_used_pct "$target")"; used="${used:-0}"
  fi
  if [ "$used" -ge "$threshold" ]; then
    warn "still ${used}% after cleanup — build may hit 'no space left on device'."
    warn "free space manually, or re-run with a higher DISK_THRESHOLD / SKIP_DISK_CHECK=1."
  else
    ok "reclaimed space — now ${used}% used"
  fi
}

# ---- arg parsing ----------------------------------------------------------
STAGE="${1:-}"; shift || true
case "$STAGE" in build|push|deploy|verify) ;; *)
  die "usage: build-deploy.sh <build|push|deploy|verify> [backend|web ...]"; esac

COMPONENTS=("$@")
[ "${#COMPONENTS[@]}" -eq 0 ] && COMPONENTS=(backend web)
for c in "${COMPONENTS[@]}"; do
  [ -n "$(img_logical "$c")" ] || die "unknown component '$c' (expected: backend web)"
done

# ---- kustomization tag helpers --------------------------------------------
# read the newTag for a logical image name out of kustomization.yaml
read_tag() {
  local logical="$1"
  awk -v img="$logical" '
    $1=="-" && $2=="name:" && $3==img {inblock=1; next}
    inblock && $1=="newTag:" {print $2; exit}
    inblock && $1=="-" {inblock=0}
  ' "$KUSTOMIZATION"
}
# vha-146 -> vha-147 ; refuses anything not matching vha-<int>
next_tag() {
  local cur="$1"
  [[ "$cur" =~ ^vha-([0-9]+)$ ]] || die "tag '$cur' is not vha-<int> — refusing to auto-increment; bump it manually."
  echo "vha-$(( ${BASH_REMATCH[1]} + 1 ))"
}
# rewrite the newTag line that follows `- name: <logical>` in place
set_tag() {
  local logical="$1" tag="$2" tmp
  if [ "${DRY_RUN:-0}" = "1" ]; then
    printf '\033[2m  $ set newTag %s -> %s in kustomization.yaml\033[0m\n' "$logical" "$tag"
    return 0
  fi
  tmp="$(mktemp)"
  awk -v img="$logical" -v newtag="$tag" '
    $1=="-" && $2=="name:" && $3==img {inblock=1}
    inblock && $1=="newTag:" { sub(/newTag:.*/, "newTag: " newtag); inblock=0 }
    {print}
  ' "$KUSTOMIZATION" > "$tmp" && mv "$tmp" "$KUSTOMIZATION"
}

# ---- verify (standalone) --------------------------------------------------
if [ "$STAGE" = "verify" ]; then
  ctx="$(kubectl config current-context 2>/dev/null || true)"
  log "kubectl context: ${ctx:-<none>}  (expected prod: $PROD_CONTEXT)"
  [ "$ctx" = "$PROD_CONTEXT" ] || warn "not on prod context — live values below are from '$ctx'."
  rc=0
  for c in "${COMPONENTS[@]}"; do
    logical="$(img_logical "$c")"; deploy="$(img_verify_deploy "$c")"
    manifest_tag="$(read_tag "$logical")"
    live_img="$(kubectl get deploy "$deploy" -n "$NAMESPACE" \
      -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
    live_tag="${live_img##*:}"
    if [ -z "$live_img" ]; then
      warn "$c: could not read live image from deploy/$deploy"
      rc=1
    elif [ "$live_tag" = "$manifest_tag" ]; then
      ok "$c: live=$live_tag == manifest=$manifest_tag"
    else
      warn "$c: live=$live_tag != manifest=$manifest_tag (cluster does not match the manifest)"
      rc=1
    fi
  done
  log "pod health in ns/$NAMESPACE (non-Running / restarts):"
  kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null | awk '
    { ready=$2; status=$3; restarts=$4; name=$1
      if (status!="Running" && status!="Completed") { print "  ! " name "  " status "  ready=" ready; bad++ }
      else if (restarts+0 > 0) { print "  ~ " name "  restarts=" restarts }
    }
    END { if (bad>0) exit 0 }' || true
  [ "$rc" -eq 0 ] && ok "verify: cluster matches manifest" || warn "verify: drift or unreadable — see above"
  exit "$rc"
fi

# next tag for a component, computed fresh from the manifest each call (no
# associative-array state — keeps this bash-3.2 safe).
component_next_tag() {
  local cur; cur="$(read_tag "$(img_logical "$1")")"
  [ -n "$cur" ] || die "could not read current tag for '$1' in kustomization"
  next_tag "$cur"
}

# ---- preview next tags for the requested components -----------------------
log "computing next tags from $(basename "$KUSTOMIZATION"):"
for c in "${COMPONENTS[@]}"; do
  cur="$(read_tag "$(img_logical "$c")")"
  printf '    %-8s %s -> %s\n' "$c" "$cur" "$(next_tag "$cur")"
done

# ---- build ----------------------------------------------------------------
ensure_disk_space
log "BUILD (linux/amd64)"
cd "$REPO_ROOT"
for c in "${COMPONENTS[@]}"; do
  local_tag="$(img_local "$c"):latest"
  log "build $c -> $local_tag"
  # shellcheck disable=SC2046,SC2086
  run docker build -f "$(img_dockerfile "$c")" "$(img_context "$c")" \
      -t "$local_tag" --platform linux/amd64 $(img_build_extra "$c")
  ok "built $local_tag"
done
[ "$STAGE" = "build" ] && { ok "build complete (no push/deploy)"; exit 0; }

# ---- push -----------------------------------------------------------------
log "PUSH -> $REGISTRY"
registry_login   # docker login using $ACR_USERNAME/$ACR_PASSWORD (see helper)
for c in "${COMPONENTS[@]}"; do
  local_tag="$(img_local "$c"):latest"
  remote_tag="$REGISTRY/$(img_logical "$c"):$(component_next_tag "$c")"
  run docker tag "$local_tag" "$remote_tag"
  log "push $remote_tag"
  if ! run docker push "$remote_tag"; then
    warn "push failed — check ACR_USERNAME/ACR_PASSWORD in ~/.zshrc, or run: az acr login --name ${REGISTRY_HOST%%.*}"
    die "aborting at push for $c"
  fi
  ok "pushed $remote_tag"
done
[ "$STAGE" = "push" ] && { ok "push complete (manifest NOT modified; run 'deploy' to roll out)"; exit 0; }

# ---- deploy ---------------------------------------------------------------
log "DEPLOY"
ctx="$(kubectl config current-context 2>/dev/null || true)"
if [ "$ctx" != "$PROD_CONTEXT" ]; then
  [ "${FORCE:-0}" = "1" ] || die "kubectl context is '$ctx', expected prod '$PROD_CONTEXT'. Switch context or set FORCE=1."
  warn "context '$ctx' != '$PROD_CONTEXT' but FORCE=1 set — proceeding."
fi
# Capture the tags BEFORE editing — set_tag mutates the manifest, so
# component_next_tag would read the already-bumped value on a second call.
APPLIED=()
for c in "${COMPONENTS[@]}"; do
  nxt="$(component_next_tag "$c")"
  set_tag "$(img_logical "$c")" "$nxt"
  APPLIED+=("$c=$nxt")
  ok "kustomization newTag $(img_logical "$c") -> $nxt"
done
log "kubectl apply -k $OVERLAY_DIR  (ns=$NAMESPACE, context=$ctx)"
run kubectl apply -k "$OVERLAY_DIR"
ok "applied. Rollout status:"
for c in "${COMPONENTS[@]}"; do
  d="$(img_verify_deploy "$c")"
  run kubectl rollout status "deploy/$d" -n "$NAMESPACE" --timeout=180s || \
    warn "rollout for $d did not complete in time — check manually"
done
warn "manifest tag bump is NOT committed. Commit it:"
printf '      git -C %s add %s && git commit -m "k8s(prod): bump %s"\n' \
  "$REPO_ROOT" "k8s/overlays/prod/kustomization.yaml" "${APPLIED[*]}"
ok "deploy complete"
