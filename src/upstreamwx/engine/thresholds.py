"""Load the externalized hazard threshold config (PRD Appendix B, FR-20a).

Thresholds live as versioned YAML under ``upstreamwx/data/thresholds/`` and are
loaded at runtime; the engine references them by key and never hard-codes a number.
Each file carries a ``version`` and a ``provenance`` block (effective date,
rationale, source) so the configured matrices are surfaceable to the user as a
"how this is calculated" reference (FR-20) and reproducible (NFR-4).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_HAZARD_FILES = {
    "flash_flood": "flash_flood.yaml",
    "lightning": "lightning.yaml",
    "heat": "heat.yaml",
    "cold_wet": "cold_wet.yaml",
    "confidence": "confidence.yaml",
}


@dataclass(frozen=True)
class HazardThresholds:
    """One hazard's configured thresholds plus its version/provenance."""

    hazard: str
    version: str
    provenance: dict
    values: dict  # remaining keys (tiers, modifiers, ...) read by the evaluator

    def __getitem__(self, key: str):
        return self.values[key]

    def get(self, key: str, default=None):
        return self.values.get(key, default)


@dataclass(frozen=True)
class ThresholdConfig:
    """The full configured threshold set, loaded once and passed to the engine."""

    flash_flood: HazardThresholds
    lightning: HazardThresholds
    heat: HazardThresholds
    cold_wet: HazardThresholds
    confidence: HazardThresholds

    @property
    def versions(self) -> dict[str, str]:
        return {
            "flash_flood": self.flash_flood.version,
            "lightning": self.lightning.version,
            "heat": self.heat.version,
            "cold_wet": self.cold_wet.version,
            "confidence": self.confidence.version,
        }

    @property
    def version(self) -> str:
        """Compact composite version string for provenance/audit (FR-20, NFR-4)."""
        return ";".join(f"{k}={v}" for k, v in self.versions.items())


def default_config_dir() -> Path:
    """Packaged threshold directory (``upstreamwx/data/thresholds``)."""
    return Path(__file__).resolve().parent.parent / "data" / "thresholds"


def _load_one(hazard: str, path: Path) -> HazardThresholds:
    if not path.is_file():
        raise FileNotFoundError(f"Missing threshold config for {hazard}: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    version = raw.pop("version", None)
    provenance = raw.pop("provenance", {})
    raw.pop("hazard", None)
    if not version:
        raise ValueError(f"Threshold config {path} is missing a 'version'")
    return HazardThresholds(
        hazard=hazard, version=str(version), provenance=provenance, values=raw
    )


def load_thresholds(config_dir: str | Path | None = None) -> ThresholdConfig:
    """Load all hazard threshold files from ``config_dir`` (default: packaged)."""
    base = Path(config_dir) if config_dir is not None else default_config_dir()
    loaded = {h: _load_one(h, base / fname) for h, fname in _HAZARD_FILES.items()}
    return ThresholdConfig(**loaded)
