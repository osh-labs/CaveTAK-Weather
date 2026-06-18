"""Terminal command: mission spec -> Markdown SITREP briefing (M0.2).

A single command turns a mission spec (point, date, window, cave/canyon) into a
complete ``.md`` briefing following the Appendix A skeleton (PRD §15). By default it
runs the live ingest pipeline (NWS / Open-Meteo / SREF / HREF / watershed) end-to-end;
``--inputs FILE`` renders from a saved :class:`HazardInputs` instead, for offline and
reproducible runs. Natural-language framing (Claude Haiku) is added when an API key is
available unless ``--no-frame`` is passed.

Examples::

    upstreamwx --lat 37.0192 --lon -111.9889 --activity canyon \\
        --start 2026-06-20T08:00 --end 2026-06-20T18:00 --name "Buckskin Gulch"

    upstreamwx --lat 37.0 --lon -112.0 --activity canyon \\
        --start 2026-06-20T08:00 --end 2026-06-20T18:00 \\
        --inputs sample_inputs.yaml --no-frame --out brief.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ..config import get_settings
from ..engine.assess import assess
from ..engine.models import ActivityType, HazardInputs, Mission
from ..ingest.base import IngestBundle
from .frame import frame_briefing
from .render import render_md


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:  # surface a clear CLI error rather than a traceback
        raise argparse.ArgumentTypeError(f"invalid datetime {value!r} (use ISO 8601)") from exc


def _load_inputs(path: Path) -> HazardInputs:
    """Load a saved HazardInputs feature vector (YAML), mirroring the corpus style."""
    data = yaml.safe_load(path.read_text())
    if isinstance(data, dict) and "inputs" in data:
        data = data["inputs"]
    return HazardInputs(**data)


def _build_mission(args: argparse.Namespace) -> Mission:
    return Mission(
        activity_type=ActivityType(args.activity),
        lat=args.lat,
        lon=args.lon,
        window_start=args.start,
        window_end=args.end,
        approach_end=args.approach_end,
        egress_start=args.egress_start,
        party_size=args.party_size,
        route_note=args.route_note,
        is_slot=args.slot,
        name=args.name,
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="upstreamwx",
        description="Generate a Markdown SITREP briefing for a caving/canyoneering mission.",
    )
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--activity", choices=[a.value for a in ActivityType], required=True)
    p.add_argument("--start", type=_parse_dt, required=True, help="window start (ISO 8601)")
    p.add_argument("--end", type=_parse_dt, required=True, help="window end (ISO 8601)")
    p.add_argument("--name", default="mission")
    p.add_argument("--approach-end", type=_parse_dt, default=None, help="phase marker (FR-9a)")
    p.add_argument("--egress-start", type=_parse_dt, default=None, help="phase marker (FR-9a)")
    p.add_argument("--party-size", type=int, default=None)
    p.add_argument("--route-note", default=None)
    p.add_argument("--slot", action="store_true", help="slot canyon (conservative flood fallback)")
    p.add_argument(
        "--inputs",
        type=Path,
        default=None,
        help="YAML HazardInputs to render from (offline; skips live ingest)",
    )
    p.add_argument("--out", type=Path, default=None, help="write .md here (default: stdout)")
    frame_group = p.add_mutually_exclusive_group()
    frame_group.add_argument(
        "--frame", dest="frame", action="store_true", default=None,
        help="add Haiku natural-language framing (requires ANTHROPIC_API_KEY)",
    )
    frame_group.add_argument(
        "--no-frame", dest="frame", action="store_false",
        help="structured render only; skip the LLM framing layer",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mission = _build_mission(args)

    upstream = None
    if args.inputs is not None:
        inputs = _load_inputs(args.inputs)
        bundle: IngestBundle | None = None
    else:
        from ..ingest.orchestrator import gather_inputs

        inputs, bundle = gather_inputs(mission)
        # The orchestrator delineates the upstream domain (NLDI raindrop two-step,
        # WBD fallback) and attaches it to the bundle; reuse it for the header so we
        # don't trace twice. None when delineation failed (header degrades, NFR-6).
        upstream = bundle.upstream if bundle is not None else None
        if upstream is None and bundle is not None and bundle.sources_ok.get("watershed") is False:
            print("warning: upstream delineation unavailable", file=sys.stderr)

    result = assess(mission, inputs)
    structured = render_md(
        result, upstream=upstream, bundle=bundle, generated_at=datetime.now(UTC)
    )

    # Frame when explicitly requested, or by default when a key is present (FR-21).
    want_frame = args.frame if args.frame is not None else bool(get_settings().anthropic_api_key)
    output = frame_briefing(result, structured) if want_frame else structured

    if args.out is not None:
        args.out.write_text(output)
        print(f"wrote briefing -> {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
