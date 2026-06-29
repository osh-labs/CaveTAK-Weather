"""Validation corpus — the oracle for "passing internal validation" (roadmap M0.1).

Boundary cases (hand-constructed inputs that sit just inside/outside each tier
edge, per hazard) are loaded from ``tests/corpus/*.yaml`` and run against the
per-hazard evaluators. This is the backbone of the engine's pass/fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from upstreamwx.engine.confidence import confidence_for
from upstreamwx.engine.hazards import cold_wet, flash_flood, heat, lightning
from upstreamwx.engine.models import Confidence, Hazard, HazardInputs, HeatCategory, Tier
from upstreamwx.engine.thresholds import load_thresholds

CORPUS_DIR = Path(__file__).parent / "corpus"


def _load_cases(filename: str) -> list:
    data = yaml.safe_load((CORPUS_DIR / filename).read_text())
    return [(filename, c) for c in data["cases"]]


def _ids(cases: list) -> list[str]:
    return [c[1]["id"] for c in cases]


CONFIG = load_thresholds()

_FLASH = _load_cases("flash_flood.yaml")
_LIGHT = _load_cases("lightning.yaml")
_HEAT = _load_cases("heat.yaml")
_COLD = _load_cases("cold_wet.yaml")
_CONF = _load_cases("confidence.yaml")


@pytest.mark.parametrize("fname,case", _FLASH, ids=_ids(_FLASH))
def test_flash_flood_corpus(fname, case):
    inputs = HazardInputs(**case["inputs"])
    tier, _drivers, _notes = flash_flood.evaluate(
        inputs, CONFIG.flash_flood, is_slot=case.get("is_slot", False)
    )
    assert tier is Tier.from_name(case["expect"]), case["id"]


@pytest.mark.parametrize("fname,case", _LIGHT, ids=_ids(_LIGHT))
def test_lightning_corpus(fname, case):
    inputs = HazardInputs(**case["inputs"])
    tier, _drivers, _notes = lightning.evaluate(inputs, CONFIG.lightning)
    assert tier is Tier.from_name(case["expect"]), case["id"]


@pytest.mark.parametrize("fname,case", _HEAT, ids=_ids(_HEAT))
def test_heat_corpus(fname, case):
    inputs = HazardInputs(**case["inputs"])
    category, _drivers, _notes = heat.evaluate(
        inputs, CONFIG.heat, is_approach=case.get("is_approach", False)
    )
    assert category is HeatCategory.from_name(case["expect"]), case["id"]


@pytest.mark.parametrize("fname,case", _COLD, ids=_ids(_COLD))
def test_cold_wet_corpus(fname, case):
    inputs = HazardInputs(**case["inputs"])
    tier, _drivers, _notes = cold_wet.evaluate(
        inputs, CONFIG.cold_wet, dry_party=case.get("dry_party", False)
    )
    assert tier is Tier.from_name(case["expect"]), case["id"]


@pytest.mark.parametrize("fname,case", _CONF, ids=_ids(_CONF))
def test_confidence_corpus(fname, case):
    inputs = HazardInputs(**case["inputs"])
    conf = confidence_for(Hazard(case["for_hazard"]), inputs, CONFIG.confidence)
    assert conf is Confidence[case["expect"].upper()], case["id"]


@pytest.mark.parametrize("mode", ["widespread", "Isolated", "", "garbage"])
def test_lightning_afd_storm_mode_degrades_gracefully(mode):
    """An out-of-vocabulary (or oddly-cased) AFD storm mode must not crash assess (NFR-6).

    The lookup is a normalized ``.get()``, so an unknown mode contributes no AFD signal
    rather than raising ``KeyError`` — known modes ("Isolated") still map after casefolding.
    """
    inputs = HazardInputs(afd_storm_mode=mode, gefs_p_tstm=10.0)
    tier, _drivers, _notes = lightning.evaluate(inputs, CONFIG.lightning)
    assert tier in set(Tier)
