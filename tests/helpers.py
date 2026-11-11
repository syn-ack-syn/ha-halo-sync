"""Test helpers shared by several test modules (importable in every pytest import mode)."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


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
