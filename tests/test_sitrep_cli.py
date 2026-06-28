"""CLI smoke tests for the offline (``--inputs``) path — no network, no LLM."""

from __future__ import annotations

from pathlib import Path

from upstreamwx.sitrep.cli import main

INPUTS = Path(__file__).parent / "fixtures" / "sitrep" / "sample_inputs.yaml"


def _args(out: Path) -> list[str]:
    return [
        "--lat", "37.0192", "--lon", "-111.9889",
        "--activity", "canyon",
        "--start", "2026-06-20T08:00", "--end", "2026-06-20T18:00",
        "--name", "Buckskin Gulch", "--slot",
        "--inputs", str(INPUTS), "--no-frame",
        "--out", str(out),
    ]


def test_cli_offline_writes_briefing(tmp_path):
    out = tmp_path / "brief.md"
    assert main(_args(out)) == 0
    text = out.read_text()
    assert text.startswith("# EXPEDITION BRIEFING")
    assert "## BLUF" in text
    assert "OVERALL POSTURE: High" in text  # slot + 65% precip drives flash flood High
    assert "## DISCLAIMER" in text
    assert "https://api.weather.gov/alerts/active" in text


def test_cli_offline_to_stdout(capsys, tmp_path):
    rc = main(_args(tmp_path / "ignored.md")[:-2])  # drop --out -> stdout
    assert rc == 0
    captured = capsys.readouterr()
    assert "# EXPEDITION BRIEFING" in captured.out
