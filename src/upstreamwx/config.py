"""Runtime configuration, loaded from environment / ``.env``.

No secrets are required for M0.0; this establishes the loading pattern (PRD §7,
secrets handling) so later milestones (e.g. the M0.2 Haiku framing key) drop in
without rework.
"""

from __future__ import annotations

from pathlib import Path

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

    # Optional override for the SREF source base URL once Spike A pins it.
    sref_base_url: str | None = None

    def ensure_data_dir(self) -> Path:
        """Create and return the data cache directory."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir


def get_settings() -> Settings:
    """Return a fresh :class:`Settings` instance (cheap; re-reads env)."""
    return Settings()
