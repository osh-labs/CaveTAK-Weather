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

# --- 1b. Unattended security updates -------------------------------------------------
# Keep the host patched without manual intervention — but SECURITY origin only (no feature
# churn that could break the GRIB stack) and NO automatic reboot: on a single-instance host
# a surprise reboot is downtime, so reboot manually when /var/run/reboot-required appears
# (see deploy/README.md). Idempotent; safe to re-run.
configure_auto_updates() {
    if command -v apt-get >/dev/null 2>&1; then
        log "enabling unattended security upgrades (apt)"
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq unattended-upgrades
        cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
        ok "unattended-upgrades enabled (security origin; manual reboot)"
    elif command -v dnf >/dev/null 2>&1; then
        log "enabling automatic security updates (dnf-automatic)"
        dnf install -y dnf-automatic >/dev/null
        sed -i 's/^apply_updates = .*/apply_updates = yes/' /etc/dnf/automatic.conf 2>/dev/null || true
        sed -i 's/^upgrade_type = .*/upgrade_type = security/' /etc/dnf/automatic.conf 2>/dev/null || true
        systemctl enable --now dnf-automatic.timer >/dev/null 2>&1 || true
        ok "dnf-automatic enabled (security; manual reboot)"
    else
        warn "no apt/dnf — configure OS auto-updates manually"
    fi
}
configure_auto_updates

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
    # -H so git runs with the service user's HOME, not the invoking sudoer's (see deploy.sh).
    sudo -u "$DEPLOY_USER" -H git clone --branch "$DEPLOY_BRANCH" "$DEPLOY_REPO_URL" "$DEPLOY_APP_DIR"
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

# Name the site after the service so a second environment (e.g. staging) installs its
# own site instead of clobbering production's (docs/deployment-workflow.md). The landing
# site (apex) installs as a SECOND site, only when DEPLOY_LANDING_SERVER_NAME is set — a
# tailnet-only staging box leaves it empty and gets just the app site.
install_nginx_site() {
    # install_nginx_site TEMPLATE SITE_NAME
    local template="$1" name="$2"
    if [ -d /etc/nginx/sites-available ]; then
        render_template "$template" "/etc/nginx/sites-available/${name}.conf"
        ln -sf "/etc/nginx/sites-available/${name}.conf" \
               "/etc/nginx/sites-enabled/${name}.conf"
    else
        # Amazon Linux / RHEL nginx uses conf.d, not sites-available.
        render_template "$template" "/etc/nginx/conf.d/${name}.conf"
    fi
}

install_nginx_site "$DEPLOY_APP_DIR/deploy/nginx/upstreamwx.conf" "$DEPLOY_SERVICE"
if [ -n "${DEPLOY_LANDING_SERVER_NAME:-}" ]; then
    install_nginx_site "$DEPLOY_APP_DIR/deploy/nginx/landing.conf" "${DEPLOY_SERVICE}-landing"
    ok "landing site enabled for: $DEPLOY_LANDING_SERVER_NAME"
else
    warn "DEPLOY_LANDING_SERVER_NAME empty — skipping the apex landing site"
fi
[ -e /etc/nginx/sites-enabled/default ] && rm -f /etc/nginx/sites-enabled/default
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
  2. Add TLS (recommended):  sudo certbot --nginx -d $DEPLOY_APP_SERVER_NAME${DEPLOY_LANDING_SERVER_NAME:+ $(printf -- '-d %s ' $DEPLOY_LANDING_SERVER_NAME)}
  3. Verify:                 curl -s http://127.0.0.1:$DEPLOY_BIND_PORT/v1/health
EOF
