"""Runtime configuration, loaded from environment / ``.env``.

No secrets are required for M0.0; this establishes the loading pattern (PRD §7,
secrets handling) so later milestones (e.g. the M0.2 Haiku framing key) drop in
without rework.
"""

from __future__ import annotations

from pathlib import Path

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

    # Optional override for the SREF source base URL once Spike A pins it.
    sref_base_url: str | None = None

    # Optional override for the HREF source base URL (same-day supplement, Spike C).
    href_base_url: str | None = None

    # The NWS API (api.weather.gov) requires a self-identifying User-Agent with a
    # contact (FR-5). Override via UPSTREAMWX_NWS_USER_AGENT to your own contact.
    nws_user_agent: str = "UpstreamWX/0.1 (+https://upstreamwx.com)"

    # Number of recent SREF cycles to retain in the on-disk grid cache before pruning
    # (roadmap §M0.1.1). 4 covers NOMADS's ~2-day retention (4 cycles/day at 03/09/15/21Z).
    sref_cache_keep_cycles: int = 4

    # Number of recent HREF *runs* (00/12Z) to retain in the on-disk grid cache (roadmap
    # §M0.1.1). 3 guarantees the previous run is present to backfill the current run's
    # spin-up hours (f01-f05) even on a missed scheduler tick or a late publish.
    href_cache_keep_cycles: int = 3

    # Start the M0.3 API's background refresh scheduler on app startup (FR-12). Default
    # on for the always-on EC2 service; set UPSTREAMWX_API_ENABLE_SCHEDULER=0 to run the
    # API without the recurring loop (e.g. tests, or a worker-less deployment).
    api_enable_scheduler: bool = True

    # Start the watershed cache-warming pool on app startup so the mission planner can
    # pre-delineate the upstream basin the moment coordinates change, hiding the 3-15 s
    # cold trace behind the user's mission-entry time (FR-3). Default on for the always-on
    # service; set UPSTREAMWX_API_ENABLE_WARM=0 for tests or a worker-less deployment.
    api_enable_warm: bool = True

    # Dead-man's-switch monitoring (Healthchecks.io or any ping-on-success service). When
    # set, the SREF/AFD refresh scheduler pings this URL each cycle (".../start" before the
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
