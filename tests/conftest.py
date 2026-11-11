"""Shared test setup.

The integration package's __init__.py imports Home Assistant. The modules under test (zones,
streamer, dtls, const) do not, so the package directory is registered here as a bare package
named ``halo_sync`` without running its __init__.py. Relative imports inside the modules
(``from .const import ...``) resolve against that package. No Home Assistant install is needed.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "custom_components" / "halo_sync"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if "halo_sync" not in sys.modules:
    _pkg = types.ModuleType("halo_sync")
    _pkg.__path__ = [str(PKG_DIR)]
    _pkg.__file__ = str(PKG_DIR / "__init__.py")
    sys.modules["halo_sync"] = _pkg


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def uniform_layer(sizes: dict[str, int], rgb: tuple[int, int, int]) -> dict:
    """A JointSpace v6 layer where every pixel has the same colour; sides with 0 pixels are left out."""
    r, g, b = rgb
    return {
        side: {str(i): {"r": r, "g": g, "b": b} for i in range(n)}
        for side, n in sizes.items()
        if n
    }


@pytest.fixture
def topology_3side() -> dict:
    return load_fixture("topology_3side.json")


@pytest.fixture
def measured_3side() -> dict:
    return load_fixture("measured_3side.json")


@pytest.fixture
def sizes_3side(topology_3side) -> dict[str, int]:
    return {side: int(topology_3side[side]) for side in ("left", "top", "right", "bottom")}
