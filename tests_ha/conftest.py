"""Setup for the tests against Home Assistant (run: `UV_PROJECT_ENVIRONMENT=.venv-ha uv run --no-default-groups --group ha pytest tests_ha`; never together with tests/ in one run).

Unlike tests/, these tests import the integration as `custom_components.halo_sync` inside a real (test)
Home Assistant from pytest-homeassistant-custom-component. The HA pytest plugin is switched off in
pyproject.toml (it blocks sockets, which the tests/ suite needs) and loaded here instead.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # custom_components/ lives in the repo root

pytest_plugins = ["pytest_homeassistant_custom_component.plugins"]

# The integration's manifest dependencies. The frontend is not installed in the test environment and
# the flow does not need any of them, so they count as set up.
_DEPENDENCIES = ("http", "frontend", "panel_custom", "websocket_api")


def pytest_configure(config: pytest.Config) -> None:
    # Home Assistant's fixtures are plain async fixtures; pytest-asyncio runs them only in auto mode.
    if config.getoption("asyncio_mode", None) is None:
        config.option.asyncio_mode = "auto"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/halo_sync."""
    return


@pytest.fixture(autouse=True)
def no_entry_setup(hass):
    """A created or reloaded entry is not really set up: no manager, no TV reader, no panel."""
    hass.config.components.update(_DEPENDENCIES)
    with (
        patch("custom_components.halo_sync.async_setup", return_value=True),
        patch("custom_components.halo_sync.async_setup_entry", return_value=True) as setup_entry,
        patch("custom_components.halo_sync.async_unload_entry", return_value=True),
    ):
        yield setup_entry
