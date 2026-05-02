#!/usr/bin/env bash
#
# Stop hook: validates whatever Claude touched in the current working tree.
# Runs once per Claude response (not per Edit), so a multi-file task
# triggers a single type-check pass rather than N redundant ones.
#
# Scope:
#   - If any web/**/*.ts(x) is dirty -> run `tsc --noEmit --incremental`
#     across the whole web project (file-scoped tsc would need its own
#     tsconfig; full project + incremental cache is the right balance).
#   - If any backend/**/*.py is dirty -> run mypy *only on those files*
#     (file-scoped mypy is fast).
#
# Output is piped through `tail` so it can't dominate Claude's context.
# Exits 0 even on failure — we want Claude to see the error in its next
# turn, not block the response from completing.
set -u

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[ -z "$PROJECT_DIR" ] && exit 0
cd "$PROJECT_DIR" || exit 0

# Modified files in the working tree (staged + unstaged + untracked).
# Filename is field 2 in `git status --porcelain` output. We strip the
# "XY " prefix manually so paths with spaces survive.
DIRTY=$(git status --porcelain 2>/dev/null | sed 's/^...//')
[ -z "$DIRTY" ] && exit 0

ts_changed=false
py_changed=()

while IFS= read -r f; do
  case "$f" in
    web/*.ts|web/*.tsx|web/**/*.ts|web/**/*.tsx) ts_changed=true ;;
    backend/*.py|backend/**/*.py) py_changed+=("${f#backend/}") ;;
  esac
done <<< "$DIRTY"

# --- TypeScript ----------------------------------------------------------
if [ "$ts_changed" = true ] && [ -d "web" ]; then
  echo "── tsc (web) ──"
  ( cd web && npx --no-install tsc --noEmit --incremental 2>&1 | tail -40 ) || true
fi

# --- Python (mypy on changed files only) ---------------------------------
# Prefer the project venv's Python (where mypy is installed). Fall back to
# python3 / python on PATH if the venv doesn't exist.
PY=""
if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  PY="$PROJECT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PY="$(command -v python)"
fi

if [ "${#py_changed[@]}" -gt 0 ] && [ -d "backend" ] && [ -n "$PY" ]; then
  echo "── mypy (changed files) ──"
  ( cd backend && "$PY" -m mypy --no-error-summary "${py_changed[@]}" 2>&1 | tail -25 ) || true
fi

exit 0
