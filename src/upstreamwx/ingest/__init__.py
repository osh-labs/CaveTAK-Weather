"""Data ingest orchestrator and provider adapters (PRD §6.2, §11.2, §12).

Each source sits behind a stable internal interface so providers can be swapped
(FR-6 Open-Meteo, FR-5 NWS, FR-7 SREF, FR-7a HREF same-day supplement, SPC outlook).
The engine consumes only the normalized :class:`IngestBundle` /
:class:`~upstreamwx.engine.models.HazardInputs`, never a raw provider (FR-13).
"""

from .base import IngestBundle, Provider, to_hazard_inputs
from .orchestrator import gather, gather_inputs

__all__ = [
    "IngestBundle",
    "Provider",
    "to_hazard_inputs",
    "gather",
    "gather_inputs",
]
