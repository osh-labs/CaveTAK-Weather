#!/usr/bin/env bash
# UpstreamWX — update + restart the backend on the host (run after bootstrap.sh).
#
#   sudo deploy/deploy.sh [git-ref]        # ref defaults to DEPLOY_BRANCH
#
# Idempotent and safe to re-run. It fetches the requested ref as the service user,
# refreshes the venv (uv), reinstalls the package, restarts the systemd service, and
# blocks on a /v1/health check so a bad deploy fails loudly instead of silently.
#
# Run on the host directly, or remotely via deploy/remote-deploy.sh from a dev machine.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"
load_config
require_root

REF="${1:-$DEPLOY_BRANCH}"
RUN_USER="sudo -u $DEPLOY_USER"

[ -d "$DEPLOY_APP_DIR/.git" ] || die "no checkout at $DEPLOY_APP_DIR — run bootstrap.sh first"
command -v uv >/dev/null 2>&1 || die "uv not found on PATH"

# --- 1. Sync source to the requested ref (branch, tag, or SHA) ------------------------
log "fetching $REF into $DEPLOY_APP_DIR"
$RUN_USER git -C "$DEPLOY_APP_DIR" fetch origin --prune --tags
if $RUN_USER git -C "$DEPLOY_APP_DIR" show-ref --verify --quiet "refs/remotes/origin/$REF"; then
    $RUN_USER git -C "$DEPLOY_APP_DIR" checkout -B "$REF" "origin/$REF"
else
    $RUN_USER git -C "$DEPLOY_APP_DIR" checkout --force "$REF"
fi
DEPLOYED_SHA="$($RUN_USER git -C "$DEPLOY_APP_DIR" rev-parse --short HEAD)"
ok "checked out $REF @ $DEPLOYED_SHA"

# --- 2. Refresh the virtualenv + install the package (production deps only) -----------
log "syncing virtualenv (uv)"
[ -d "$DEPLOY_APP_DIR/.venv" ] || $RUN_USER uv venv --python 3.11 "$DEPLOY_APP_DIR/.venv"
$RUN_USER env VIRTUAL_ENV="$DEPLOY_APP_DIR/.venv" \
    uv pip install --python "$DEPLOY_APP_DIR/.venv/bin/python" -e "$DEPLOY_APP_DIR"
ok "dependencies installed"

# Fail fast if the GRIB stack can't import (the most likely host-specific breakage).
if ! $RUN_USER "$DEPLOY_APP_DIR/.venv/bin/python" -c "import cfgrib" 2>/dev/null; then
    warn "cfgrib failed to import — check ecCodes (see deploy/README.md troubleshooting)"
fi

# --- 3. Restart the service ----------------------------------------------------------
log "restarting $DEPLOY_SERVICE"
systemctl restart "$DEPLOY_SERVICE"

# --- 4. Health check -----------------------------------------------------------------
log "waiting for /v1/health"
url="http://${DEPLOY_BIND_HOST}:${DEPLOY_BIND_PORT}/v1/health"
for i in $(seq 1 20); do
    if curl -fsS "$url" >/dev/null 2>&1; then
        ok "healthy: $(curl -fsS "$url")"
        log "deployed $REF @ $DEPLOYED_SHA"
        exit 0
    fi
    sleep 1
done

warn "service did not become healthy in time — recent logs:"
journalctl -u "$DEPLOY_SERVICE" -n 40 --no-pager || true
die "deploy failed health check"
