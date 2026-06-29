#!/usr/bin/env bash
# UpstreamWX — update + restart the backend on the host (run after bootstrap.sh).
#
#   sudo deploy/deploy.sh [git-ref]        # ref defaults to DEPLOY_BRANCH
#
# Idempotent and safe to re-run. It fetches the requested ref as the service user,
# refreshes the venv (uv), reinstalls the package, restarts the systemd service, and
# blocks on a /v1/health check so a bad deploy fails loudly instead of silently.
#
# Run on the server (SSH in, then invoke it); the server pulls its own code from git.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"
load_config
require_root

REF="${1:-$DEPLOY_BRANCH}"
# -H sets HOME to the service user's home ($DEPLOY_APP_DIR); without it sudo keeps the
# invoking user's HOME (/home/ubuntu), which the service user can't read — uv then fails
# to open its config/cache there (Permission denied).
RUN_USER="sudo -u $DEPLOY_USER -H"

[ -d "$DEPLOY_APP_DIR/.git" ] || die "no checkout at $DEPLOY_APP_DIR — run bootstrap.sh first"
command -v uv >/dev/null 2>&1 || die "uv not found on PATH"

# Run from inside the app dir. uv discovers config by walking UP from the CWD; if invoked
# from the sudoer's home (/home/ubuntu, mode 0750) the service user can't read it and uv
# dies with "failed to open uv.toml: Permission denied". The app dir is service-readable.
cd "$DEPLOY_APP_DIR"

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

# --- 1b. Stamp the release into frontend/version.json --------------------------------
# A single source of truth for "what's deployed": prefer the nearest tag (production
# deploys a tag), else the short SHA. /v1/health echoes it for ops, and the PWA polls
# version.json to nudge stale clients to reload after a release (docs/deployment-workflow.md).
# It's git-ignored and untracked, so the checkout above never clobbers or conflicts with it.
RELEASE="$($RUN_USER git -C "$DEPLOY_APP_DIR" describe --tags --always 2>/dev/null || echo "$DEPLOYED_SHA")"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
$RUN_USER tee "$DEPLOY_APP_DIR/frontend/version.json" >/dev/null <<EOF
{"version": "$RELEASE", "sha": "$DEPLOYED_SHA", "built_at": "$BUILT_AT"}
EOF
ok "stamped release $RELEASE"

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

# Install a Chromium binary for server-side PDF export (FR-27, sitrep/pdf.py).
#
# Strategy: try Playwright's own managed Chromium first (preferred — version-pinned,
# known-good with the installed Playwright Python package).  If the distro isn't
# supported yet (e.g. Ubuntu 26.04 before Playwright catches up), fall back to the
# system Chromium from apt.  pdf.py searches both locations via _chromium_path().
PLAYWRIGHT_BROWSERS_DIR="$DEPLOY_APP_DIR/.playwright-browsers"
log "ensuring Chromium is available for PDF export"
if $RUN_USER env PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_DIR" \
        "$DEPLOY_APP_DIR/.venv/bin/playwright" install chromium 2>/dev/null; then
    ok "Playwright Chromium ready at $PLAYWRIGHT_BROWSERS_DIR"
else
    warn "playwright install chromium failed (unsupported distro?) — trying system Chromium"
    # chromium / chromium-browser are interchangeable names across distros.
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq chromium 2>/dev/null \
            || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq chromium-browser 2>/dev/null \
            || warn "system Chromium unavailable — PDF export endpoint will return 503"
    elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
        "${PKG:-dnf}" install -y chromium >/dev/null 2>&1 \
            || warn "system Chromium unavailable — PDF export endpoint will return 503"
    fi
    if command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1; then
        ok "system Chromium available for PDF export"
    else
        warn "no Chromium found — PDF export endpoint will return 503"
    fi
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
