#!/usr/bin/env bash
# UpstreamWX — one-time host provisioning for the always-on backend (roadmap §M0.1.1).
#
# Run ONCE as root on a fresh EC2 host. Idempotent: safe to re-run. It installs system
# packages, the service account, the directory layout, the systemd unit and nginx site,
# then hands off to deploy.sh to build the venv, install the app, and start the service.
#
#   sudo deploy/bootstrap.sh
#
# Configure the target first by copying deploy/config.env.example -> deploy/config.env.
# Tested on Ubuntu/Debian (apt); Amazon Linux notes are in deploy/README.md.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"
load_config
require_root

# --- 1. System packages --------------------------------------------------------------
# shapely/pyproj/geopandas ship manylinux wheels that bundle GEOS/GDAL/PROJ, so the only
# GRIB/geo system library cfgrib genuinely benefits from is ecCodes (FR: SREF/HREF GRIB2).
install_packages_apt() {
    log "installing system packages (apt)"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq \
        git curl ca-certificates build-essential \
        nginx libeccodes0
    ok "apt packages installed"
}

install_packages_dnf() {
    log "installing system packages (dnf/yum)"
    "$PKG" install -y git curl ca-certificates gcc gcc-c++ make nginx >/dev/null
    # ecCodes is not in the default Amazon Linux repos; the `eccodes` PyPI wheel bundles
    # the binary as a fallback. See deploy/README.md if cfgrib fails to import.
    "$PKG" install -y eccodes >/dev/null 2>&1 || warn "system eccodes unavailable — relying on the pip wheel"
    ok "dnf packages installed"
}

if command -v apt-get >/dev/null 2>&1; then
    install_packages_apt
elif command -v dnf >/dev/null 2>&1; then
    PKG=dnf install_packages_dnf
elif command -v yum >/dev/null 2>&1; then
    PKG=yum install_packages_dnf
else
    die "no supported package manager (apt/dnf/yum) found"
fi

# --- 2. uv (Python toolchain, matches the repo) --------------------------------------
if ! command -v uv >/dev/null 2>&1; then
    log "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi
command -v uv >/dev/null 2>&1 || die "uv install failed; install it manually and re-run"
ok "uv: $(uv --version)"

# --- 3. Service account + directories ------------------------------------------------
if ! getent group "$DEPLOY_GROUP" >/dev/null; then
    groupadd --system "$DEPLOY_GROUP"
fi
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    log "creating service user $DEPLOY_USER"
    useradd --system --gid "$DEPLOY_GROUP" --home-dir "$DEPLOY_APP_DIR" \
            --shell /usr/sbin/nologin "$DEPLOY_USER"
fi
install -d -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" -m 0755 "$DEPLOY_APP_DIR"
install -d -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" -m 0750 "$DEPLOY_DATA_DIR"
install -d -o root -g "$DEPLOY_GROUP" -m 0750 "$DEPLOY_ENV_DIR"
ok "user + directories ready"

# --- 4. Source checkout --------------------------------------------------------------
if [ ! -d "$DEPLOY_APP_DIR/.git" ]; then
    log "cloning $DEPLOY_REPO_URL ($DEPLOY_BRANCH) -> $DEPLOY_APP_DIR"
    sudo -u "$DEPLOY_USER" git clone --branch "$DEPLOY_BRANCH" "$DEPLOY_REPO_URL" "$DEPLOY_APP_DIR"
else
    ok "repo already present at $DEPLOY_APP_DIR (deploy.sh will update it)"
fi

# --- 5. Runtime env file (install once; never clobber live secrets) ------------------
if [ ! -f "$DEPLOY_ENV_FILE" ]; then
    log "installing runtime env file -> $DEPLOY_ENV_FILE"
    install -o root -g "$DEPLOY_GROUP" -m 0640 \
        "$DEPLOY_APP_DIR/deploy/upstreamwx.env.example" "$DEPLOY_ENV_FILE"
    warn "edit $DEPLOY_ENV_FILE — set NWS contact and (optional) ANTHROPIC_API_KEY"
else
    ok "env file exists at $DEPLOY_ENV_FILE (left untouched)"
fi

# --- 6. systemd unit + nginx site (rendered from templates) --------------------------
log "installing systemd unit and nginx site"
render_template "$DEPLOY_APP_DIR/deploy/systemd/upstreamwx-api.service" \
                "/etc/systemd/system/${DEPLOY_SERVICE}.service"

# Render the nginx site ONLY if it doesn't already exist. Re-running bootstrap must not
# clobber a certbot-managed config: `certbot --nginx` rewrites this file in place to add
# the :443/TLS block, and overwriting it back to the HTTP-only template silently breaks
# HTTPS (browsers then fall back to the cached service worker and deploys never appear).
install_nginx_site() {
    local dest="$1"
    if [ -f "$dest" ]; then
        warn "nginx site already exists at $dest — preserving it (keeps any certbot TLS)."
        warn "  delete it and re-run bootstrap to re-render from the template."
    else
        render_template "$DEPLOY_APP_DIR/deploy/nginx/upstreamwx.conf" "$dest"
        ok "rendered nginx site -> $dest"
    fi
}

if [ -d /etc/nginx/sites-available ]; then
    install_nginx_site "/etc/nginx/sites-available/upstreamwx.conf"
    ln -sf /etc/nginx/sites-available/upstreamwx.conf /etc/nginx/sites-enabled/upstreamwx.conf
    [ -e /etc/nginx/sites-enabled/default ] && rm -f /etc/nginx/sites-enabled/default
else
    # Amazon Linux / RHEL nginx uses conf.d, not sites-available.
    install_nginx_site "/etc/nginx/conf.d/upstreamwx.conf"
fi
systemctl daemon-reload
if nginx -t >/dev/null 2>&1; then
    systemctl enable --now nginx >/dev/null 2>&1 || true
    systemctl reload nginx
    ok "nginx configured"
else
    warn "nginx -t failed — review the site config before reloading nginx"
fi

# --- 7. Build venv, install app, start the service (delegated to deploy.sh) ----------
log "running deploy.sh for the initial build + service start"
"$DEPLOY_APP_DIR/deploy/deploy.sh" "$DEPLOY_BRANCH"

cat <<EOF

$(ok "bootstrap complete")

Next steps:
  1. Edit secrets/contact:   sudo nano $DEPLOY_ENV_FILE
                             sudo systemctl restart $DEPLOY_SERVICE
  2. Add TLS (recommended):  sudo certbot --nginx -d $DEPLOY_SERVER_NAME
  3. Verify:                 curl -s http://127.0.0.1:$DEPLOY_BIND_PORT/v1/health
EOF
