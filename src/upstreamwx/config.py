"""Runtime configuration, loaded from environment / ``.env``.

No secrets are required for M0.0; this establishes the loading pattern (PRD §7,
secrets handling) so later milestones (e.g. the M0.2 Haiku framing key) drop in
without rework.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration sourced from env vars and an optional ``.env`` file."""

    model_config = SettingsConfigDict(
        env_prefix="UPSTREAMWX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Root for runtime caches (raw GRIB2 pulls, WBD downloads). Git-ignored.
    data_dir: Path = Path("./data")

    # Static PWA directory served single-origin by the API (M0.4). None -> the repo's
    # ``frontend/`` (resolved relative to the package); set UPSTREAMWX_FRONTEND_DIR to
    # override for a packaged deployment, or to "" to disable static serving entirely.
    frontend_dir: Path | None = None

    # Optional override for the GEFS source base URL (default: NOMADS gens/prod, the operational
    # GEFS endpoint; SREF replacement). Rarely needed — GEFS is already on its production path.
    gefs_base_url: str | None = None

    # REFS source feed (HREF replacement, NWS SCN 26-48). Selects the (base URL, ensemble-product
    # subdir) pair the REFS provider reads:
    #   "aws"         -> noaa-rrfs-pds/rrfs_a + "enspost"  (RRFS *prototype* bucket; validated)
    #   "nomads_para" -> com/refs/para       + "ensprod"  (NOMADS pre-implementation parallel)
    #   "nomads_prod" -> com/refs/prod       + "ensprod"  (NOMADS production, live 2026-08-31 12Z)
    # CUTOVER: flip this to "nomads_prod" once the NOMADS REFS feed is confirmed reachable and its
    # ``ensprod`` layout validated from a network-unrestricted host. Default "aws" is the only feed
    # reachable for end-to-end validation pre-cutover.
    refs_source: Literal["aws", "nomads_para", "nomads_prod"] = "aws"
    # Raw overrides (take precedence over ``refs_source``); leave None to use the profile above.
    refs_base_url: str | None = None
    # Ensemble-product subdir override ("enspost" on AWS, "ensprod" on NOMADS).
    refs_subdir: str | None = None

    # The NWS API (api.weather.gov) requires a self-identifying User-Agent with a
    # contact (FR-5). Override via UPSTREAMWX_NWS_USER_AGENT to your own contact.
    nws_user_agent: str = "UpstreamWX/0.1 (+https://upstreamwx.com)"

    # Number of recent GEFS cycles to retain in the on-disk member cache before pruning.
    # 4 covers ~1 day of GEFS's four daily cycles (00/06/12/18Z).
    gefs_cache_keep_cycles: int = 4

    # Freshness bound (hours since run init) for serving a cached ensemble cycle as "current".
    # Both ensembles run 6-hourly with a ~4-8 h publication lag, so a healthy system serves runs
    # <= ~14 h old; 24 h tolerates a missed warm without letting a stalled scheduler or a
    # long-idle CLI data_dir quietly present days-old members as the live ensemble. Past the
    # bound GEFS falls through to a live NOMADS probe and REFS degrades to "unavailable" with
    # an explicit note (data quality first-class, NFR-6).
    ensemble_max_age_h: float = 24.0

    # Number of recent REFS *runs* (00/06/12/18Z) to retain in the on-disk grid cache. 3
    # guarantees the previous run is present to backfill the current run's spin-up hours
    # even on a missed scheduler tick or a late publish.
    refs_cache_keep_cycles: int = 3

    # GEFS forecast hours the scheduler (and deploy.sh) pre-warm each cycle. GEFS is per-member
    # (31×fields), so warming is heavy — but leaving it on-demand puts the full ~500-subset cold
    # ingest on the first briefing's critical path every cycle. Default to the f24-f120 / 6 h band
    # (the multi-day planning horizon GEFS owns beyond REFS's ~36 h window); warming is download-
    # only and fanned across a thread pool (:func:`upstreamwx.gefs.cache.warm_cycle`). Override
    # with UPSTREAMWX_GEFS_WARM_FHOURS=[...] (e.g. [] to revert to fully on-demand).
    gefs_warm_fhours: list[int] = Field(default_factory=lambda: list(range(24, 121, 6)))

    # Start the M0.3 API's background refresh scheduler on app startup (FR-12). Default
    # on for the always-on EC2 service; set UPSTREAMWX_API_ENABLE_SCHEDULER=0 to run the
    # API without the recurring loop (e.g. tests, or a worker-less deployment).
    api_enable_scheduler: bool = True

    # Start the watershed cache-warming pool on app startup so the mission planner can
    # pre-delineate the upstream basin the moment coordinates change, hiding the 3-15 s
    # cold trace behind the user's mission-entry time (FR-3). Default on for the always-on
    # service; set UPSTREAMWX_API_ENABLE_WARM=0 for tests or a worker-less deployment.
    api_enable_warm: bool = True

    # Decode GEFS GRIB2 in a persistent process pool (spawn) so the per-member decodes run truly
    # in parallel — eccodes is not thread-safe, so the in-process path serializes them on a global
    # lock. **Opt-in (default off):** each spawn worker is a fresh interpreter that re-imports the
    # whole scientific stack (xarray + cfgrib/eccodes + regionmask/rasterio, and the package chain
    # pulls in timezonefinder) — ~300–500 MB RSS *per worker* on top of the main process. On the
    # small (≤2 GB) production host that OOM-killed uvicorn (→ nginx 502). Only enable
    # (UPSTREAMWX_API_ENABLE_DECODE_POOL=1, and tune UPSTREAMWX_DECODE_POOL_WORKERS) on a host with
    # real RAM headroom; otherwise the single-interpreter in-process decode is the safe path and
    # GEFS *warming* (off the request path) is the latency win.
    api_enable_decode_pool: bool = False
    # Worker count for the decode pool when enabled. Keep small — see the RSS-per-worker note above.
    decode_pool_workers: int = Field(default_factory=lambda: min(4, (os.cpu_count() or 2)))
    # Resident byte budget for the in-process decoded-grid LRU (memory-aware eviction). GEFS decodes
    # are cropped at decode time (~KB each), so this mainly bounds the larger REFS native grids; a
    # conservative 128 MiB keeps the resident cache safe on a ≤2 GB host (raise where RAM allows).
    # Replaces the old flat 48-entry count cap that retained full grids.
    decode_cache_max_bytes: int = 128 * 1024 * 1024

    # Hard entry cap for the in-process briefing cache and the engine-result store (M0.3).
    # Both are keyed by mission and were previously unbounded dicts — on an always-on host
    # they grew with every distinct mission for the process lifetime (a slow leak). Bounding
    # them as LRUs caps memory; an evicted entry simply regenerates on the next request, and
    # an evicted engine result makes the streaming frame endpoint a graceful miss (NFR-6).
    # 512 distinct missions is generous for a single-host beta; raise where RAM allows.
    api_cache_max_entries: int = 512

    # Cap the active-mission refresh registry (H-8). refresh_active re-ingests every registered
    # mission each cycle, so scheduler cost scales linearly with the registry — which previously
    # grew without bound (entries only dropped when their window ended). At the cap the service
    # evicts the mission whose window ends soonest; an evicted mission still briefs on demand,
    # it just loses scheduled refresh (NFR-6). 256 in-range missions is generous for one host.
    api_active_missions_max: int = 256

    # Cap the watershed warm queue (H-8): each pending warm is a 3-15 s USGS delineation, and the
    # pending set previously grew (and queued executor work) without bound. At the cap the service
    # refuses new warms (endpoint -> 503 + Retry-After); the briefing then just pays the cold
    # trace itself (NFR-6). 32 pending points is far beyond any legitimate planner burst.
    api_warm_pending_max: int = 32

    # Per-IP token-bucket rate limiting on the expensive/billable endpoints (frame/pdf/warm),
    # H-8. In-process and dependency-free; nginx's edge limit_req still applies in front
    # (deploy/nginx/upstreamwx.conf) — this is the app's own defence when reached directly.
    # Set UPSTREAMWX_API_RATE_LIMITS_ENABLED=0 to disable (e.g. load tests).
    api_rate_limits_enabled: bool = True

    # Cap concurrent live briefing GENERATIONS (the cold, memory/CPU-heavy ingest path) so a burst
    # of simultaneous distinct missions can't OOM/thrash a small host — excess requests wait briefly
    # for a slot, then return a fast 503 "busy, retry" (the PWA shows a retry banner) instead of all
    # spiking at once. Cache hits never count against this. ~2 suits a 2-vCPU/4 GiB box; raise where
    # there is headroom. Set 0 to disable the cap.
    briefing_max_concurrency: int = 2
    # Seconds a queued generation waits for a free slot before returning 503. A short wait lets a
    # quick collision resolve (and possibly hit the cache the in-flight request fills); past it the
    # client is told to retry rather than risk overrunning the gateway timeout (a 504).
    briefing_busy_timeout_s: float = 2.0

    # Dead-man's-switch monitoring (Healthchecks.io or any ping-on-success service). When
    # set, the GEFS/REFS + AFD refresh scheduler pings this URL each cycle (".../start" before the
    # pass, the base URL on success, ".../fail" on error). A stalled scheduler — stale
    # briefings with no error, the most dangerous failure mode on the always-on host — then
    # raises an alert instead of going unnoticed. Unset -> no pings. The ping URL is a
    # secret; keep it in the env file, never in git.
    healthcheck_url: str | None = None

    # Anthropic API key for the M0.2 SITREP Haiku framing layer (FR-21). Read from the
    # standard ANTHROPIC_API_KEY name; the validation_alias bypasses the env_prefix above.
    anthropic_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY")
    )

    def ensure_data_dir(self) -> Path:
        """Create and return the data cache directory."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir


def get_settings() -> Settings:
    """Return a fresh :class:`Settings` instance (cheap; re-reads env)."""
    return Settings()
