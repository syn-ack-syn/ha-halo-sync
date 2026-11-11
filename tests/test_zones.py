"""Zone sampling (zones.py) on the target TV's ring: left 4, top 8, right 4, no bottom."""

from __future__ import annotations

import pytest

from conftest import uniform_layer
from halo_sync import zones
from halo_sync.zones import NATIVE_SIZES, SIDES, ZoneSampler, ring_colors, ring_sizes, weights

COLOR = (200, 100, 50)
EXPECTED = [v / 255 for v in COLOR]
MODES = ("smooth", "nearest", "some", "side", "native")

# Light positions on the unit screen square: every side, the corners, the centre and the bottom
# edge (where the target TV has no pixels).
POSITIONS = [(-1.0, 0.0), (0.0, 1.0), (1.0, 0.0), (0.0, -1.0), (-1.0, -1.0), (1.0, 1.0), (0.0, 0.0), (0.5, -1.2)]


def _sample(layer, mode, positions=POSITIONS, zones_=None, saturation=1.0, count=3):
    zones_ = zones_ if zones_ is not None else [None] * len(positions)
    return ZoneSampler().sample(layer, positions, mode, 0.8, count, zones_, saturation)


def test_fixture_matches_topology(measured_3side, sizes_3side):
    layer = measured_3side["layer1"]
    assert "bottom" not in layer
    assert ring_sizes(layer) == (4, 8, 4, 0)
    assert ring_sizes(layer) == tuple(sizes_3side[s] for s in SIDES)
    assert len(ring_colors(layer)) == 16


@pytest.mark.parametrize("mode", MODES)
def test_uniform_edge_gives_that_color(mode, sizes_3side):
    layer = uniform_layer(sizes_3side, COLOR)
    for rgb in _sample(layer, mode):
        assert rgb == pytest.approx(EXPECTED)


@pytest.mark.parametrize("mode", MODES)
def test_weights_sum_to_one_without_bottom(mode):
    sizes = (4, 8, 4, 0)
    for x, z in POSITIONS:
        w = weights(mode, sizes, x, z, 0.8, 3)
        assert len(w) == 16
        assert sum(w) == pytest.approx(1.0)
        assert all(v >= 0 for v in w)


@pytest.mark.parametrize("mode", MODES)
def test_missing_bottom_does_not_pull_in_black(mode, sizes_3side):
    """A light at the bottom edge follows the other sides instead of averaging in zeros."""
    layer = uniform_layer(sizes_3side, COLOR)
    bottom_lights = [(0.0, -1.0), (-0.6, -1.0), (0.6, -1.0)]
    for rgb in _sample(layer, mode, positions=bottom_lights):
        assert rgb == pytest.approx(EXPECTED)


@pytest.mark.parametrize("mode", MODES)
def test_empty_bottom_key_behaves_like_missing(mode, sizes_3side):
    """Some firmwares may send "bottom": {} instead of leaving the key out."""
    layer = uniform_layer(sizes_3side, COLOR)
    layer["bottom"] = {}
    assert ring_sizes(layer) == (4, 8, 4, 0)
    for rgb in _sample(layer, mode):
        assert rgb == pytest.approx(EXPECTED)


@pytest.mark.parametrize("mode", MODES)
def test_fixture_runs_in_every_mode(mode, measured_3side):
    out = _sample(measured_3side["layer1"], mode)
    assert len(out) == len(POSITIONS)
    for rgb in out:
        assert len(rgb) == 3
        assert all(0.0 <= v <= 1.0 for v in rgb)


def test_nearest_follows_the_closest_side(measured_3side):
    left, top, right = _sample(measured_3side["layer1"], "nearest", positions=[(-1.0, 0.0), (0.0, 1.0), (1.0, 0.0)])
    assert max(left) == left[0]  # left side is red in the fixture
    assert max(top) == top[1]  # top is green
    assert max(right) == right[2]  # right is blue


def test_some_averages_k_pixels():
    sizes = (4, 8, 4, 0)
    w = weights("some", sizes, 0.0, 1.0, 0.8, 2)
    assert sum(1 for v in w if v) == 2
    w = weights("some", sizes, 0.0, 1.0, 0.8, 99)  # clamped to the side length
    assert sum(1 for v in w if v) == 8


def test_side_uses_the_whole_side():
    w = weights("side", (4, 8, 4, 0), -1.0, 0.0)
    assert w[:4] == pytest.approx([0.25] * 4)
    assert sum(w[4:]) == 0


def test_native_on_other_ring_degrades_to_nearest():
    sizes = (4, 8, 4, 0)
    for x, z in [(-1.0, 0.3), (0.2, 1.0), (1.0, -0.4)]:
        assert weights("native", sizes, x, z) == weights("nearest", sizes, x, z)


@pytest.mark.parametrize("zone", sorted(zones.NATIVE_ZONES))
def test_native_zones_on_native_ring_give_that_color(zone):
    layer = uniform_layer(dict(zip(SIDES, NATIVE_SIZES)), COLOR)
    (rgb,) = _sample(layer, "native", positions=[(0.0, 0.0)], zones_=[zone])
    assert rgb == pytest.approx(EXPECTED)


def test_saturation_scales_distance_from_grey():
    layer = uniform_layer({"left": 4, "top": 8, "right": 4}, COLOR)
    (base,) = _sample(layer, "side", positions=[(-1.0, 0.0)])
    (half,) = _sample(layer, "side", positions=[(-1.0, 0.0)], saturation=0.5)
    (boost,) = _sample(layer, "side", positions=[(-1.0, 0.0)], saturation=2.0)
    m = sum(base) / 3
    assert half == pytest.approx([m + (v - m) * 0.5 for v in base])
    assert boost == pytest.approx([min(1.0, max(0.0, m + (v - m) * 2.0)) for v in base])


@pytest.mark.parametrize("saturation", [0.5, 1.0, 2.0])
def test_saturation_keeps_grey_and_range(saturation):
    grey = uniform_layer({"left": 4, "top": 8, "right": 4}, (128, 128, 128))
    (rgb,) = _sample(grey, "smooth", positions=[(0.0, 1.0)], saturation=saturation)
    assert rgb == pytest.approx([128 / 255] * 3)
    vivid = uniform_layer({"left": 4, "top": 8, "right": 4}, (255, 0, 0))
    (rgb,) = _sample(vivid, "smooth", positions=[(0.0, 1.0)], saturation=saturation)
    assert all(0.0 <= v <= 1.0 for v in rgb)
