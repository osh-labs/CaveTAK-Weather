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
