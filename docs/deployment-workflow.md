# Deployment & release workflow

How code gets from your laptop to clients, safely. UpstreamWX is a life-safety
reference app, so the guiding rule is absolute:

> **Production is a deployment target, not a workspace.** Nobody edits code on the
> prod server, ever. The server only ever runs an exact, named version from git, so
> "what's live" is always knowable and always revertible.

Everything below is the machinery that makes that rule livable.

## The environment ladder

| Env | Where | Purpose |
| --- | --- | --- |
| **Local** | your laptop, the offline `--inputs` path | write + test changes (network-free, deterministic) |
| **Staging** | its own EC2 instance, reachable **only over Tailscale** | final check against *live* data before clients see it |
| **Production** | the always-on box at `upstreamwx.com` | clients |

Local → staging → production, in increasing blast radius. Staging runs the
*candidate* version against the *real* upstream feeds (NWS/SREF/HREF), catching
"works on fixtures, breaks on today's actual GRIB" before a caver does.

Staging gets its **own instance** (not a second service on the prod box) so its load
and any bad deploy can't degrade production, and it's **tailnet-only** — no public DNS,
no public 80/443, reached via the node's Tailscale MagicDNS name. It need not be
always-on; stop it between release validations. See
[`deploy/README.md`](../deploy/README.md#staging-a-pre-production-mirror) for standing one up.

## Branching model (GitHub Flow)

1. **`main` is always deployable.** Never commit to it directly.
2. Every change is a short-lived **feature branch** off `main`.
3. Open a **PR** → CI runs ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml):
   `ruff` + the hermetic `pytest` suite). Review your own diff with fresh eyes.
4. **Merge to `main`** only when CI is green.
5. `main` deploys to **staging**.
6. **Tag a release** (`vX.Y.Z`) to promote that exact commit to **production**.

The key discipline: *merging to main is not the same as going live.* main → staging
is routine; staging → prod is a deliberate, tagged act.

### Make CI a required check (one-time, repo owner)

CI runs automatically, but it can't *block* a merge until you require it:

- repo **Settings → Branches → Add branch protection rule**, branch name `main`
- enable **Require status checks to pass before merging** → select **`test`**
- (recommended) **Require a pull request before merging**

Now red code physically cannot reach `main`, and therefore cannot reach a release tag.

## Cutting a release (staging → production)

Production deploys an **immutable tag**, never a moving branch — so "what's in prod"
is a fixed, knowable thing and rollback is trivial.

```sh
# 1. main is green and verified on staging. Tag it:
git tag -a v0.5.0 -m "v0.5.0"
git push origin v0.5.0

# 2. On the prod box, deploy that exact tag:
sudo deploy/deploy.sh v0.5.0
```

`deploy.sh` fetches the ref, rebuilds the venv, restarts the service, and **blocks on
`/v1/health`** — a bad deploy fails loudly instead of half-landing. It also stamps the
deployed release into `frontend/version.json` (git-ignored), which `/v1/health` echoes
back and the PWA uses to nudge stale clients to reload.

### Rollback

Because every release is a tag, reverting is just deploying the previous one:

```sh
sudo deploy/deploy.sh v0.4.9
```

Confirm the running version any time with `curl -s localhost:8000/v1/health` (the
`release` field).

## PWA / client-update notes

An installed PWA is cached software on someone's phone, so "I deployed the fix" ≠
"clients have the fix." Two mechanisms keep clients current:

- **Cache busting is tied to the release.** The page registers `sw.js?v=<release>`,
  so each new release is a new service-worker URL → the SW reinstalls and evicts the
  old caches automatically. No more manual `VERSION` bump in `sw.js`.
- **Stale-client nudge.** The app records the release it booted with and re-checks
  `version.json`; when the deployed release changes, a **non-dismissible "Update
  available — reload"** banner appears. Reloading picks up the new shell.

When you change the `/v1/briefing` JSON shape, **add fields, don't repurpose them** —
an old cached client may still be reading the old shape until it reloads.

## Observability (do this before real users depend on it)

- **Scheduler dead-man's-switch** — the most important one for this app. A silently
  stalled refresh scheduler serves **stale briefings with no error**, the worst failure
  mode. Set `UPSTREAMWX_HEALTHCHECK_URL` in the env file to a Healthchecks.io ping URL;
  the scheduler pings it each cycle (`.../start` before, base on success, `.../fail` on
  error). Configure the check's period to your SREF cycle (~6 h) plus grace, and a missed
  ping alerts you. Wired in `api/scheduler.py`.
- **External uptime check** hitting `/v1/health` (UptimeRobot / Healthchecks.io) that
  alerts you when the box or the health check goes down.
- **Error visibility** — at minimum `journalctl -u upstreamwx-api`; ideally Sentry so a
  500 in `engine.assess` reaches you before a user does.
- The **deploy health gate** (`deploy.sh` blocking on `/v1/health`) is already in place.

## Host upkeep

- **OS security patching** — `bootstrap.sh` enables unattended **security** upgrades
  (apt `unattended-upgrades` / dnf `dnf-automatic`), with **automatic reboot off**: on a
  single-instance host a surprise reboot is downtime, so reboot manually when
  `/var/run/reboot-required` appears.
- **TLS renewal** — prod's certbot installs its own renewal timer (`certbot.timer`);
  confirm with `sudo certbot renew --dry-run`. Tailnet staging via `tailscale serve` is
  auto-renewed by Tailscale.

## Secrets

Never commit secrets. Runtime secrets live in the per-environment `EnvironmentFile`
(`/etc/upstreamwx/upstreamwx.env`, mode 0640) — staging and prod get separate files.
CI/CD secrets live in GitHub Actions Secrets, not the repo.
