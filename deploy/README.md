# Deploying UpstreamWX

This folder deploys the UpstreamWX briefing backend to the always-on host the PRD
assumes (PRD §7, roadmap §M0.1.1) — the EC2 instance whose job is to keep a current
briefing cache warm on the SREF/AFD cycle (FR-12).

It installs **one** service: the FastAPI app (`upstreamwx-api`), run as a single uvicorn
process under systemd, with **nginx** in front for TLS. The API serves the PWA
single-origin (M0.4), so there is no separate frontend deployment — the same process
answers `/v1/briefing`, `/v1/health`, and the static PWA.

The app lives on its **own subdomain** (`app.upstreamwx.com`); nginx serves a small
**static landing page** at the apex (`upstreamwx.com` + `www`) from `landing/` in the
checkout. Both names go in **one** multi-SAN TLS cert. Set the names in `config.env`
(`DEPLOY_APP_SERVER_NAME`, `DEPLOY_LANDING_SERVER_NAME`); leave the landing name empty to
serve app-only (e.g. tailnet staging).

> **Single process, on purpose.** The app's lifespan starts the in-process SREF-cycle
> refresh scheduler (`api/app.py` → `scheduler.run_scheduler`). That scheduler is a
> singleton — running multiple uvicorn workers would multiply the refresh passes. Scale
> by host size, not worker count. If you ever need multiple API workers, run them with
> `UPSTREAMWX_API_ENABLE_SCHEDULER=0` and a single dedicated scheduler process.

## What gets installed

| Path | What |
| --- | --- |
| `/opt/upstreamwx` | git checkout + `.venv` (owned by the `upstreamwx` service user) |
| `/var/lib/upstreamwx` | runtime cache (`UPSTREAMWX_DATA_DIR`) — survives redeploys |
| `/etc/upstreamwx/upstreamwx.env` | runtime env + secrets (`EnvironmentFile`, mode 0640) |
| `/etc/systemd/system/upstreamwx-api.service` | the service unit |
| `/etc/nginx/.../upstreamwx-api.conf` | app site: reverse proxy (`:80` → `127.0.0.1:8000`) for `app.upstreamwx.com` |
| `/etc/nginx/.../upstreamwx-api-landing.conf` | landing site: static apex (`upstreamwx.com` + `www`) from `landing/` |

## Files in this folder

| File | Role |
| --- | --- |
| `config.env.example` | deploy target (repo, branch, paths, server name) — copy to `config.env` |
| `upstreamwx.env.example` | runtime env/secrets template → `/etc/upstreamwx/upstreamwx.env` |
| `bootstrap.sh` | **one-time** server provisioning (run as root on the server) |
| `deploy.sh` | update + restart on the server (run for every release) |
| `systemd/upstreamwx-api.service` | systemd unit template (`__TOKENS__` rendered at install) |
| `nginx/upstreamwx.conf` | nginx site template |
| `_lib.sh` | shared logging / config / template rendering (sourced, not run) |

The `systemd` and `nginx` files are **templates**: `bootstrap.sh` substitutes the
`__TOKENS__` from `config.env` and installs the rendered copies into `/etc`. Edit
`config.env`, not the system files, then re-run `bootstrap.sh`.

---

## Deployment model

Everything runs **on the server**: you SSH in, clone the repo to get the scripts, and
run them there. There is no push-from-laptop step — the server pulls its own code from
git. You need: an EC2 instance (Ubuntu/Debian recommended), a sudo login, DNS A/AAAA
records for `app.upstreamwx.com`, `upstreamwx.com`, and `www.upstreamwx.com` all pointed
at it, and security-group ingress on 80/443.

## First-time install

```sh
# SSH into the server, then:
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/osh-labs/upstreamwx.git /tmp/upstreamwx-src
cd /tmp/upstreamwx-src

# Configure the target (server names, paths, branch). The defaults already point at
# app.upstreamwx.com (app) + upstreamwx.com/www (landing) and the standard /opt + /var/lib
# layout, so usually no edits are needed.
cp deploy/config.env.example deploy/config.env
nano deploy/config.env          # optional: override DEPLOY_BRANCH, server names, paths, etc.

# Provision: system packages, user, dirs, systemd + nginx, venv, first start.
sudo deploy/bootstrap.sh
```

`bootstrap.sh` clones the repo into `/opt/upstreamwx` (the permanent location — the
`/tmp` copy was just to get these scripts), then hands off to `deploy.sh` to build the
venv and start the service. It is idempotent; re-run it any time.

Then finish the two manual steps it prints:

```sh
# 1. Secrets + NWS contact (required: NWS rejects requests without a real UA — FR-5).
sudo nano /etc/upstreamwx/upstreamwx.env       # set NWS contact; add ANTHROPIC_API_KEY
sudo systemctl restart upstreamwx-api

# 2. TLS (strongly recommended). One multi-SAN cert covers the app + landing names;
#    certbot rewrites both nginx sites in place.
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d app.upstreamwx.com -d upstreamwx.com -d www.upstreamwx.com
```

Verify:

```sh
curl -s  http://127.0.0.1:8000/v1/health         # on the server
curl -s  https://app.upstreamwx.com/v1/health    # the app
curl -sI https://upstreamwx.com/                 # the static landing (200)
```

`/v1/health` returns the current refresh cycle and cache size — proof the scheduler and
cache are live.

---

## Routine deploys (every release)

The server already has everything; a deploy just moves it to a new git ref and restarts.
SSH in and run `deploy.sh` (it lives in the checkout from the first install):

```sh
sudo /opt/upstreamwx/deploy/deploy.sh main        # or any branch / tag / commit SHA
```

`deploy.sh` fetches the ref as the service user, refreshes the venv (`uv pip install
-e .`), restarts the service, and **blocks on `/v1/health`** — a deploy that doesn't
come up healthy exits non-zero and dumps the last 40 journal lines, so it fails loudly.
It also stamps the deployed release into `frontend/version.json` (git-ignored), which
`/v1/health` echoes back as `release` and the PWA polls to nudge stale clients to reload.

**Production deploys a tag, not a branch.** A tag is immutable, so "what's in prod" is a
fixed, knowable thing and rollback is just deploying the previous tag. Deploying a moving
branch works but leaves prod tracking whatever that branch points at right now. See
[`../docs/deployment-workflow.md`](../docs/deployment-workflow.md) for the full release flow.

Roll back by deploying an older tag or SHA:

```sh
sudo /opt/upstreamwx/deploy/deploy.sh v0.3.1
sudo /opt/upstreamwx/deploy/deploy.sh 1a2b3c4
```

---

## Staging (a pre-production mirror)

Run a second environment to validate a release candidate against **live** data before
clients see it. For a life-safety app, give staging its **own instance** (not a second
service on the prod box) so a staging load spike or a bad staging deploy can never touch
production. Staging need not be always-on — use a smaller instance and/or stop it between
release validations; only prod needs to stay up to hold a warm cache.

### Access: tailnet-only (no public exposure)

Staging is reached over **Tailscale**, not the public internet. That gates access by
*device* (works from any machine/network you're signed in on, no password, no IP
allowlist) and means staging has **zero public attack surface** and can't be indexed or
stumbled onto — which matters: a publicly reachable staging copy of a hazard-reference
app is itself a hazard. There is **no public DNS record, no security-group 80/443 rule,
and no certbot** for staging.

```sh
# On the staging box, join your tailnet (and use Tailscale SSH so you can close port 22):
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
# Then in the Tailscale admin console: enable MagicDNS + HTTPS certificates.
```

The PWA's service worker requires a secure origin (HTTPS) even on the tailnet. Two ways:

- **Simplest — `tailscale serve`** (Tailscale terminates TLS, proxies straight to the
  uvicorn backend; auto-managed cert, auto-renew, no nginx cert wrangling):
  ```sh
  sudo tailscale serve --bg 8000          # serves https://<node>.<tailnet>.ts.net
  ```
- **Full nginx fidelity — `tailscale cert` + nginx** (keeps nginx in the request path,
  matching prod): issue a cert for the node's MagicDNS name and point a TLS server block
  at it, renewing on a timer:
  ```sh
  sudo tailscale cert <node>.<tailnet>.ts.net   # writes <name>.crt / <name>.key
  # add an nginx :443 server_name <node>.<tailnet>.ts.net block using those files,
  # and a daily systemd-timer/cron that re-runs `tailscale cert` to renew.
  ```

### Provision + deploy

Because staging is its **own box**, it doesn't need the same-box `DEPLOY_CONFIG`
machinery — it just has its own `config.env`:

```sh
# One-time:
git clone https://github.com/osh-labs/upstreamwx.git /tmp/upstreamwx-src && cd /tmp/upstreamwx-src
cp deploy/config.env.example deploy/config.env
# DEPLOY_BRANCH="main"; DEPLOY_APP_SERVER_NAME="<node>.<tailnet>.ts.net"; and
# DEPLOY_LANDING_SERVER_NAME="" so staging is app-only (no public apex landing).
nano deploy/config.env
sudo deploy/bootstrap.sh
sudo nano /etc/upstreamwx/upstreamwx.env     # NWS UA identifying staging; ANTHROPIC_API_KEY optional
sudo systemctl restart upstreamwx-api

# Each release candidate (staging tracks main):
sudo deploy/deploy.sh                          # ref defaults to DEPLOY_BRANCH=main
curl -s http://127.0.0.1:8000/v1/health        # release should show main's short SHA
```

To conserve cost, set `UPSTREAMWX_API_ENABLE_SCHEDULER=0` in the staging env file — staging
then generates on demand rather than holding a warm cache. Promote to production by
**tagging** the commit you verified on staging and deploying that tag on the prod box
(`sudo deploy/deploy.sh v0.5.0`).

> **Expected on a scheduler-off staging box: HREF may show degraded.** HREF backfills the
> current run's spin-up hours (f01–f05) from the *previous* cached run (`href_cache_keep_cycles`).
> A cold box with the scheduler off never accumulates that prior run, so when the current
> run isn't fully published yet, HREF can't be assembled and the briefing reports
> `sources_ok.href = false` (graceful degradation, NFR-6 — the engine still postures from
> SREF). This is **not a fault and not a staging misconfiguration**; prod hides it because
> its always-on scheduler keeps the prior run warm. The tell that it's this and not a
> network problem: SREF (same NOMADS source) still succeeds. To make staging match prod
> here, set `UPSTREAMWX_API_ENABLE_SCHEDULER=1` and let it warm over a cycle or two.

> **Same-box variant.** If you ever want staging and prod on one host instead, the scripts
> still support it: `cp deploy/config.staging.env.example deploy/config.staging.env` and
> run them with `DEPLOY_CONFIG=deploy/config.staging.env` (distinct service/port/paths so
> the two don't collide). The dedicated-instance model above is preferred for this app.

---

## Operating the service

```sh
systemctl status upstreamwx-api            # state
journalctl -u upstreamwx-api -f            # live logs (scheduler refreshes log here)
sudo systemctl restart upstreamwx-api      # apply env changes
sudo systemctl stop upstreamwx-api         # take it down
```

- **Logs:** the scheduler logs each cycle's regenerated-briefing count; the app logs
  the resolved PWA directory and degraded sources.
- **Config changes** (`/etc/upstreamwx/upstreamwx.env`) require a restart.
- **The data cache** at `/var/lib/upstreamwx` is intentionally outside the code tree, so
  redeploys never clear it — important given NOMADS's ~2-day SREF retention.

### Monitoring & host upkeep

- **Scheduler heartbeat (recommended for prod):** set `UPSTREAMWX_HEALTHCHECK_URL` in the
  env file to a [Healthchecks.io](https://healthchecks.io) ping URL. The scheduler pings it
  each cycle (`.../start`, base on success, `.../fail` on error), so a silently stalled
  scheduler — stale briefings with no error — raises an alert. Set the check's period to
  your SREF cycle (~6 h) plus a grace window. Restart the service after setting it.
- **OS security patches:** `bootstrap.sh` enables unattended **security** upgrades with
  **auto-reboot off**. Check `cat /var/run/reboot-required` and reboot manually during a
  quiet window when a kernel update needs it.
- **TLS renewal** is automatic — certbot's timer for prod (`sudo certbot renew --dry-run`
  to verify); Tailscale auto-renews staging when served via `tailscale serve`.

---

## Amazon Linux / RHEL notes

`bootstrap.sh` auto-detects `dnf`/`yum` and uses nginx's `conf.d/` layout. The one
caveat is **ecCodes**: it isn't in the default Amazon Linux repos. The `eccodes` PyPI
package ships a bundled binary wheel that usually suffices, and `deploy.sh` import-checks
`cfgrib` after install and warns if it fails. If it does fail, install ecCodes from
source or EPEL, or run on Ubuntu where `libeccodes0` is a one-line `apt` install. The
geo stack (GEOS/GDAL/PROJ) needs no system packages — shapely/pyproj/geopandas ship
manylinux wheels that bundle them.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `deploy.sh` warns `cfgrib failed to import` | ecCodes missing — see the Amazon Linux note above |
| Service flaps / restarts | `journalctl -u upstreamwx-api -n 80`; usually a bad value in the env file |
| `/v1/health` 502 from nginx | service down or wrong `DEPLOY_BIND_PORT`; check `systemctl status` |
| Deploy succeeds but the PWA looks unchanged in the browser | The server *is* updated — it's the client service-worker cache. Confirm the server first: `curl -s https://<host>/v1/health` (check `release`) and `curl -s https://<host>/version.json`. If those show the new release, the open tab will surface an "Update available — reload" banner the next time it re-checks `version.json` (when you switch back to the tab, or within a few minutes); reloading registers `sw.js?v=<release>`, which reinstalls the SW and evicts the old caches. As of the network-first shell, deploys also propagate on the next manual reload regardless. |
| NWS ingest empty / 403 | set a real contact in `UPSTREAMWX_NWS_USER_AGENT` (FR-5) and restart |
| No framed summary in briefings | `ANTHROPIC_API_KEY` unset — expected; the structured posture is unaffected |
| `certbot` can't bind :80 | open the security group / firewall on 80 and 443 |
| Briefing requests return **429** | nginx rate limit on `/v1/briefing` + `/v1/watershed/warm` (2 r/s, burst 10, per IP). Normal use never hits it; tune `rate`/`burst` in `deploy/nginx/upstreamwx.conf` if needed, then re-run `bootstrap.sh`. |

## Security notes

- uvicorn binds **loopback only**; the public surface is nginx (80/443). Keep it that
  way — don't expose `8000` in the security group.
- Secrets live only in `/etc/upstreamwx/upstreamwx.env` (0640, `root:upstreamwx`).
  `deploy/config.env` and any real env file are git-ignored.
- The systemd unit is hardened (`ProtectSystem=strict`, `NoNewPrivileges`,
  `PrivateTmp`, writable path restricted to the data dir).
- nginx **rate-limits the expensive endpoints** (`/v1/briefing`, `/v1/watershed/warm`)
  per client IP, so a request flood can't exhaust the single uvicorn process and DoS the
  box. Static assets and `/v1/health` are unthrottled.
- nginx sends baseline **security headers** (`X-Content-Type-Options`, `Referrer-Policy`,
  `X-Frame-Options`, and HSTS once TLS is on via certbot).
