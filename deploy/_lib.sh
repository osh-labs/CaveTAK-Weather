#!/usr/bin/env bash
# Shared helpers for the UpstreamWX deploy scripts. Sourced, not executed.
#
# Provides: coloured logging, config loading (deploy/config.env with env overrides),
# and __TOKEN__ rendering for the systemd/nginx templates. Keeping this in one place
# means bootstrap.sh and deploy.sh agree on paths by construction.

set -euo pipefail

# Directory containing the deploy scripts, regardless of the caller's CWD.
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"

_have_tty() { [ -t 1 ]; }
log()  { if _have_tty; then printf '\033[1;34m==>\033[0m %s\n' "$*"; else printf '==> %s\n' "$*"; fi; }
ok()   { if _have_tty; then printf '\033[1;32m  ✓\033[0m %s\n' "$*"; else printf '  OK %s\n' "$*"; fi; }
warn() { if _have_tty; then printf '\033[1;33m  ! \033[0m%s\n' "$*" >&2; else printf '  ! %s\n' "$*" >&2; fi; }
die()  { if _have_tty; then printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; else printf 'error: %s\n' "$*" >&2; fi; exit 1; }

# Load the deploy config if present, then let real environment variables win (so
# `DEPLOY_BRANCH=foo ./deploy.sh` overrides the file). Defaults below cover a fresh
# checkout where the config file hasn't been created yet.
#
# Which file? `config.env` by default; set DEPLOY_CONFIG to point at another (e.g.
# `sudo DEPLOY_CONFIG=deploy/config.staging.env deploy/deploy.sh v0.5.0`) so ONE set of
# scripts drives both staging and production, each with its own service name / port /
# paths (docs/deployment-workflow.md).
load_config() {
    local cfg="${DEPLOY_CONFIG:-$DEPLOY_DIR/config.env}"
    if [ -n "${DEPLOY_CONFIG:-}" ] && [ ! -f "$cfg" ]; then
        die "DEPLOY_CONFIG=$cfg not found"
    fi
    if [ -f "$cfg" ]; then
        # shellcheck disable=SC1090
        set -a; source "$cfg"; set +a
    fi
    : "${DEPLOY_REPO_URL:=https://github.com/osh-labs/upstreamwx.git}"
    : "${DEPLOY_BRANCH:=main}"
    : "${DEPLOY_APP_DIR:=/opt/upstreamwx}"
    : "${DEPLOY_USER:=upstreamwx}"
    : "${DEPLOY_GROUP:=upstreamwx}"
    : "${DEPLOY_DATA_DIR:=/var/lib/upstreamwx}"
    : "${DEPLOY_ENV_DIR:=/etc/upstreamwx}"
    : "${DEPLOY_ENV_FILE:=/etc/upstreamwx/upstreamwx.env}"
    : "${DEPLOY_BIND_HOST:=127.0.0.1}"
    : "${DEPLOY_BIND_PORT:=8000}"
    : "${DEPLOY_SERVER_NAME:=upstreamwx.com}"
    : "${DEPLOY_SERVICE:=upstreamwx-api}"
}

# render_template SRC DEST — substitute the __TOKEN__ placeholders into DEST.
render_template() {
    local src="$1" dest="$2"
    sed -e "s|__USER__|${DEPLOY_USER}|g" \
        -e "s|__GROUP__|${DEPLOY_GROUP}|g" \
        -e "s|__APP_DIR__|${DEPLOY_APP_DIR}|g" \
        -e "s|__DATA_DIR__|${DEPLOY_DATA_DIR}|g" \
        -e "s|__ENV_FILE__|${DEPLOY_ENV_FILE}|g" \
        -e "s|__BIND_HOST__|${DEPLOY_BIND_HOST}|g" \
        -e "s|__BIND_PORT__|${DEPLOY_BIND_PORT}|g" \
        -e "s|__SERVER_NAME__|${DEPLOY_SERVER_NAME}|g" \
        "$src" > "$dest"
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root (use sudo)"
}
