"""Historical-replay corpus — the "realism check" half of the M0.1 validation
oracle (roadmap M0.1).

Where ``test_engine_corpus.py`` drives each per-hazard evaluator across its tier
edges in isolation, this runs whole documented missions end-to-end through
``engine.assess`` and asserts the overall posture plus the documented dominant
hazard(s). Cases live in ``tests/corpus/historical_replay.yaml`` with provenance.
This is the regression backbone for "the engine flags the right tier on a real,
multi-hazard situation," extended at every later milestone (roadmap cross-cutting).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from upstreamwx.engine.assess import assess
from upstreamwx.engine.models import ActivityType, Hazard, HazardInputs, Mission
from upstreamwx.engine.thresholds import load_thresholds

CORPUS_DIR = Path(__file__).parent / "corpus"
CONFIG = load_thresholds()


def _load_cases(filename: str) -> list:
    data = yaml.safe_load((CORPUS_DIR / filename).read_text())
    return [(filename, c) for c in data["cases"]]


_REPLAY = _load_cases("historical_replay.yaml")


def _build_mission(spec: dict) -> Mission:
    return Mission(
        activity_type=ActivityType(spec["activity_type"]),
        lat=spec["lat"],
        lon=spec["lon"],
        window_start=datetime.fromisoformat(spec["window_start"]),
        window_end=datetime.fromisoformat(spec["window_end"]),
        is_slot=spec.get("is_slot", False),
        name=spec.get("name", "replay"),
    )


@pytest.mark.parametrize("fname,case", _REPLAY, ids=[c[1]["id"] for c in _REPLAY])
def test_historical_replay(fname, case):
    result = assess(
        _build_mission(case["mission"]),
        HazardInputs(**case["inputs"]),
        CONFIG,
    )
    expect = case["expect"]

    assert result.overall_tier.label.lower() == expect["overall"].lower(), (
        f"{case['id']}: overall {result.overall_tier.label} != {expect['overall']}"
    )

    for hazard_name, want in (expect.get("hazards") or {}).items():
        hazard = Hazard(hazard_name)
        assert hazard in result.bluf, f"{case['id']}: {hazard_name} absent from BLUF"
        got = result.bluf[hazard].severity_label
        assert got.lower() == want.lower(), (
            f"{case['id']}: {hazard_name} {got} != {want}"
        )
