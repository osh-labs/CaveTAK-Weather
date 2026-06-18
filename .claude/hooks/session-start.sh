#!/bin/bash
# SessionStart hook: install UpstreamWX dependencies so pytest/ruff work
# out of the box in Claude Code on the web. Synchronous + idempotent.
set -euo pipefail

# Only run in the remote (web) environment; local setups manage their own venv.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Create the virtualenv once (idempotent), then install the package with dev
# extras. `uv pip install` targets the project's .venv automatically.
[ -d .venv ] || uv venv
uv pip install -e '.[dev]'

# Put the venv on PATH for the rest of the session so `pytest` and `ruff`
# resolve without the .venv/bin prefix.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export VIRTUAL_ENV=\"$CLAUDE_PROJECT_DIR/.venv\"" >> "$CLAUDE_ENV_FILE"
  echo "export PATH=\"$CLAUDE_PROJECT_DIR/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi
