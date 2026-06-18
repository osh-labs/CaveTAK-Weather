"""Shared pytest fixtures and markers for the test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Ensure the `network` marker is registered (also declared in pyproject)."""
    config.addinivalue_line(
        "markers",
        "network: test hits live services (NOMADS/AWS/USGS); deselected by default",
    )


@pytest.fixture
def fixtures_dir() -> Path:
    """Absolute path to tests/fixtures (committed offline sample data)."""
    return Path(__file__).parent / "fixtures"
