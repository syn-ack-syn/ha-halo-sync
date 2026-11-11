"""Recorded JointSpace v6 responses of a 2018 Philips 55PUS7373 (tools/capture_fixture.py).

Skipped until the fixtures exist: they are recorded on the real TV and committed separately.
"""

from __future__ import annotations

import json
import re

import pytest

from helpers import FIXTURES, load_fixture

DIR = FIXTURES / "55pus7373"

if not (DIR / "topology.json").exists():
    pytest.skip("55PUS7373 fixtures not recorded yet (run tools/capture_fixture.py)", allow_module_level=True)

from halo_sync.zones import SIDES, ZoneSampler, ring_colors, ring_sizes  # noqa: E402

MODES = ("smooth", "nearest", "some", "side", "native")
POSITIONS = [(-1.0, 0.0), (0.0, 1.0), (1.0, 0.0), (0.0, -1.0), (-1.0, -1.0), (1.0, 1.0), (0.0, 0.0)]
# The first recording (2026-11-11) was made with a static picture: measured_00..19 and
# processed_00..19 are byte-identical within each endpoint. They still cover parsing and every
# zone mode; they stay until the next recording with a moving picture replaces them.
SAMPLES = sorted(DIR.glob("measured_*.json")) + sorted(DIR.glob("processed_*.json"))
IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
MAC_RE = re.compile(r"\b[0-9a-f]{2}([:-])[0-9a-f]{2}(?:\1[0-9a-f]{2}){4}\b", re.IGNORECASE)


def _load(path) -> dict:
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def topology() -> dict:
    return _load(DIR / "topology.json")


def _sizes(topology: dict) -> tuple[int, ...]:
    return tuple(int(topology.get(side, 0)) for side in SIDES)


def test_samples_present():
    assert sorted(DIR.glob("measured_*.json")), "no measured samples"
    assert sorted(DIR.glob("processed_*.json")), "no processed samples"


def test_topology_matches_synthetic_assumption(topology):
    """AP-2 assumed left 4, top 8, right 4, no bottom; the real TV must agree."""
    assert _sizes(topology) == _sizes(load_fixture("topology_3side.json"))
    assert str(topology["layers"]) == "1"


def test_powerstate_on():
    assert _load(DIR / "powerstate_on.json") == {"powerstate": "On"}


def test_system_is_anonymised():
    system = _load(DIR / "system.json")
    assert set(system) <= {"api_version", "model"}
    if "model" in system:  # the 2018 firmware sends the model only encrypted, then it is left out
        assert "55PUS7373" in system["model"]
    assert int(system["api_version"]["Major"]) == 6


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_sample_parses_and_matches_topology(path, topology):
    data = _load(path)
    assert set(data) == {"layer1"}
    layer = data["layer1"]
    assert not layer.get("bottom"), "the target TV has no bottom Ambilight"
    assert ring_sizes(layer) == _sizes(topology)
    colors = ring_colors(layer)
    assert len(colors) == sum(_sizes(topology))
    assert all(0 <= v <= 255 for c in colors for v in c)


@pytest.mark.parametrize("mode", MODES)
def test_every_mode_runs_on_every_sample(mode):
    sampler = ZoneSampler()
    for path in SAMPLES:
        out = sampler.sample(_load(path)["layer1"], POSITIONS, mode, 0.8, 3, [None] * len(POSITIONS), 1.0)
        assert len(out) == len(POSITIONS)
        assert all(0.0 <= v <= 1.0 for rgb in out for v in rgb), path.name


def test_timing_has_both_endpoints():
    timing = _load(DIR / "timing.json")
    assert timing["picture"] in ("static", "moving")
    for part in ("fixture_samples", "timing_run"):
        assert set(timing[part]) == {"measured", "processed"}
    for ep, v in timing["timing_run"].items():
        assert v["response_ms"] is not None, f"{ep}: no successful answer"
        assert {"min", "median", "p95", "max"} <= set(v["response_ms"])
        assert len(v["changes_per_second"]) == len(v["answers_per_second"])


def test_static_capture_has_moving_reference():
    """A static picture says nothing about the update rate, so a moving-picture run must be on record."""
    timing = _load(DIR / "timing.json")
    if timing["picture"] != "static":
        pytest.skip("timing run was made with a moving picture")
    ref = timing["moving_reference"]
    assert ref["picture"] == "moving"
    for ep in ("measured", "processed"):
        assert 0 < ref[ep]["changes"] <= ref[ep]["requests"]
        assert {"median", "p95"} <= set(ref[ep]["response_ms"])


def test_no_personal_data_in_fixtures():
    files = sorted(DIR.glob("*.json"))
    assert files
    for f in files:
        text = f.read_text()
        assert not IP_RE.search(text), f"IP address in {f.name}"
        assert not MAC_RE.search(text), f"MAC address in {f.name}"
        low = text.lower()
        for word in ("serialnumber", "user", "password"):
            assert word not in low, f"{word!r} in {f.name}"
