# Deploying UpstreamWX

This folder deploys the UpstreamWX briefing backend to the always-on host the PRD
assumes (PRD §7, roadmap §M0.1.1) — the EC2 instance whose job is to keep a current
briefing cache warm on the SREF/AFD cycle (FR-12).

It installs **one** thing: the FastAPI service (`upstreamwx-api`), run as a single
uvicorn process under systemd, with **nginx** in front for TLS. The API serves the PWA
single-origin (M0.4), so there is no separate frontend deployment — the same process
answers `/v1/briefing`, `/v1/health`, and the static PWA.

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
| `/etc/nginx/.../upstreamwx.conf` | reverse proxy (`:80` → `127.0.0.1:8000`) |

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
git. You need: an EC2 instance (Ubuntu/Debian recommended), a sudo login, DNS pointed at
it (`upstreamwx.com`), and security-group ingress on 80/443.

## First-time install

```sh
# SSH into the server, then:
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/osh-labs/upstreamwx.git /tmp/upstreamwx-src
cd /tmp/upstreamwx-src

# Configure the target (server name, paths, branch). The defaults already point at
# upstreamwx.com and the standard /opt + /var/lib layout, so usually no edits are needed.
cp deploy/config.env.example deploy/config.env
nano deploy/config.env          # optional: override DEPLOY_BRANCH, paths, etc.

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

# 2. TLS (strongly recommended). certbot rewrites the nginx site in place.
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d upstreamwx.com
```

Verify:

```sh
curl -s http://127.0.0.1:8000/v1/health        # on the server
curl -s https://upstreamwx.com/v1/health
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

Run a second, identical environment to test a release candidate against **live** data
before clients see it. The same scripts drive it — point them at a staging config with
`DEPLOY_CONFIG`. The staging config uses a distinct service name, port, paths, and server
name, so it coexists with production (same box or a separate one) without collision.

```sh
# One-time, on the box:
cp deploy/config.staging.env.example deploy/config.staging.env
nano deploy/config.staging.env                                      # set server name, etc.
sudo DEPLOY_CONFIG=deploy/config.staging.env deploy/bootstrap.sh
sudo nano /etc/upstreamwx-staging/upstreamwx.env                    # NWS contact + secrets
sudo systemctl restart upstreamwx-staging

# Each release candidate (staging tracks main):
sudo DEPLOY_CONFIG=deploy/config.staging.env deploy/deploy.sh main
curl -s http://127.0.0.1:8001/v1/health
```

Promote to production by tagging the commit you verified on staging and deploying that
tag with the **default** config (`sudo deploy/deploy.sh v0.5.0`). To conserve resources
you can set `UPSTREAMWX_API_ENABLE_SCHEDULER=0` in the staging env file — it just means
staging generates on demand rather than holding a warm cache.

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

## Security notes

- uvicorn binds **loopback only**; the public surface is nginx (80/443). Keep it that
  way — don't expose `8000` in the security group.
- Secrets live only in `/etc/upstreamwx/upstreamwx.env` (0640, `root:upstreamwx`).
  `deploy/config.env` and any real env file are git-ignored.
- The systemd unit is hardened (`ProtectSystem=strict`, `NoNewPrivileges`,
  `PrivateTmp`, writable path restricted to the data dir).
