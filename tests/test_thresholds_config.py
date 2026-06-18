"""Threshold config tests — externalized, versioned, provenance-bearing (FR-20a).

Also guards the "thresholds are data, not code" principle: the hazard evaluator
modules must not contain bare numeric tier cut points — those live only in YAML.
"""

from __future__ import annotations

import tokenize
from pathlib import Path

import pytest

from upstreamwx.engine.thresholds import (
    default_config_dir,
    load_thresholds,
)

HAZARDS = ["flash_flood", "lightning", "heat", "cold_wet", "confidence"]


def test_config_loads_with_versions():
    cfg = load_thresholds()
    assert set(cfg.versions) == set(HAZARDS)
    assert all(v for v in cfg.versions.values())
    assert "flash_flood=" in cfg.version


@pytest.mark.parametrize("hazard", HAZARDS)
def test_each_hazard_has_provenance(hazard):
    cfg = load_thresholds()
    ht = getattr(cfg, hazard)
    for key in ("effective_date", "rationale", "source"):
        assert ht.provenance.get(key), f"{hazard} missing provenance.{key}"


def test_override_config_dir(tmp_path):
    # Tuning is config-only: an edited copy changes behavior with no code change.
    src = default_config_dir()
    dst = tmp_path / "thresholds"
    dst.mkdir()
    for f in src.glob("*.yaml"):
        text = f.read_text()
        if f.name == "flash_flood.yaml":
            text = text.replace("high_min: 60", "high_min: 50")
        (dst / f.name).write_text(text)

    from upstreamwx.engine.hazards import flash_flood
    from upstreamwx.engine.models import HazardInputs, Tier

    cfg = load_thresholds(dst)
    inputs = HazardInputs(sref_p_precip=55, measurable_precip=True)
    tier, _d, _n = flash_flood.evaluate(inputs, cfg.flash_flood)
    assert tier is Tier.HIGH  # 55% now clears the lowered 50% High threshold


def test_evaluators_have_no_hardcoded_tier_cutpoints():
    # The numeric tier/probability/temperature cut points must come from config.
    # Tokenize so docstrings, PRD references, and string drivers are ignored — we
    # only flag genuine numeric *literals* in code (which would be inlined thresholds).
    hazard_dir = Path(__file__).parent.parent / "src" / "upstreamwx" / "engine" / "hazards"
    offenders = []
    for py in hazard_dir.glob("*.py"):
        with tokenize.open(py) as fh:
            for tok in tokenize.generate_tokens(fh.readline):
                # Allow trivial structural literals (indices, identity arithmetic);
                # any other numeric literal would be an inlined threshold.
                if tok.type == tokenize.NUMBER and tok.string not in {"0", "1"}:
                    offenders.append(f"{py.name}:{tok.start[0]}: {tok.string}")
    assert not offenders, "Hard-coded numeric literals in evaluators:\n" + "\n".join(offenders)
