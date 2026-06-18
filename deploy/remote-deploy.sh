#!/usr/bin/env bash
# UpstreamWX — trigger a deploy on the EC2 host from your dev machine.
#
#   deploy/remote-deploy.sh [git-ref]      # ref defaults to DEPLOY_BRANCH
#
# Thin convenience wrapper: it SSHes to DEPLOY_SSH_HOST and runs deploy.sh on the host
# (which pulls the ref from the git remote — code is NOT copied over ssh, the host pulls
# it itself). Configure the target in deploy/config.env before first use.
#
# This does NOT provision the host. Run bootstrap.sh on the host once, first.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"
load_config

[ -n "${DEPLOY_SSH_HOST:-}" ] || die "set DEPLOY_SSH_HOST in deploy/config.env"
REF="${1:-$DEPLOY_BRANCH}"

log "deploying '$REF' to $DEPLOY_SSH_HOST"
# shellcheck disable=SC2086 — DEPLOY_SSH_OPTS is intentionally word-split.
ssh ${DEPLOY_SSH_OPTS:-} "$DEPLOY_SSH_HOST" \
    "sudo ${DEPLOY_APP_DIR}/deploy/deploy.sh $(printf '%q' "$REF")"
ok "remote deploy finished"
