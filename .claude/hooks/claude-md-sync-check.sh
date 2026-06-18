#!/bin/bash
# SessionStart check: warn when CLAUDE.md may have drifted from the spec and
# source it documents (milestone status, repo layout, commands, conventions).
#
# It is a *reminder*, not a gate: it never blocks the session and never edits
# anything. When watched files have changed since CLAUDE.md was last updated, it
# emits a SessionStart `additionalContext` note so the agent reviews CLAUDE.md
# (especially the "Milestone status" section) and keeps it in sync. Read-only and
# idempotent; runs in both local and web sessions.
set -euo pipefail

cd "${CLAUDE_PROJECT_DIR:-.}"

# Only meaningful inside a git repo with a committed CLAUDE.md.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
[ -f CLAUDE.md ] || exit 0

# Paths whose changes most often make CLAUDE.md stale: the governing docs, the
# milestone findings, the backend package, and the build/lint/run config.
WATCH=(roadmap.md UpstreamWX-PRD-v0.8.md docs src/upstreamwx pyproject.toml)

# Commit that last touched CLAUDE.md; the baseline we compare everything against.
base=$(git log -1 --format=%H -- CLAUDE.md 2>/dev/null || true)
[ -n "$base" ] || exit 0

# Watched files changed in commits since that baseline, plus any uncommitted
# working-tree edits to the same paths.
committed=$(git diff --name-only "$base" HEAD -- "${WATCH[@]}" 2>/dev/null || true)
dirty=$(git status --porcelain -- "${WATCH[@]}" 2>/dev/null | awk '{print $NF}' || true)

changed=$(printf '%s\n%s\n' "$committed" "$dirty" | sed '/^$/d' | sort -u)
[ -n "$changed" ] || exit 0

count=$(printf '%s\n' "$changed" | wc -l | tr -d ' ')
list=$(printf '%s\n' "$changed" | head -40 | sed 's/^/  - /')

msg="CLAUDE.md sync check: ${count} spec/source file(s) changed since CLAUDE.md was last updated (baseline ${base:0:8}). CLAUDE.md documents the milestone status, repo layout, commands, and conventions for this repo — verify it is still accurate before relying on it, and update it in the same change whenever you alter milestones, module layout, commands, or conventions. Pay special attention to the \"Milestone status\" section. Changed paths:
${list}"

# Emit the reminder as SessionStart additionalContext (jq escapes safely;
# python3 is the fallback). Any other exit path prints nothing => no reminder.
if command -v jq >/dev/null 2>&1; then
  jq -n --arg c "$msg" \
    '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$c}}'
else
  esc=$(printf '%s' "$msg" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
  printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":%s}}\n' "$esc"
fi
