"""Tone curves (streamer.tone) and temporal smoothing (_Smoother, _NativeFilter, _Ramp)."""

from __future__ import annotations

import pytest

from halo_sync.const import CHROMA_FULL_LEVEL, DARK_CONTENT_LEVELS, DARK_LEVEL, TUNABLES
from halo_sync.streamer import _WARM_WHITE, _NativeFilter, _Ramp, _Smoother, tone


def preset(**over) -> dict:
    s = {
        "source": "video",
        "dark_content": "off",
        "brightness": 100,
        "min_brightness": 0,
        "gain": 1.0,
        "curve": 1.0,
        "saturation": 1.0,
    }
    s.update(over)
    return s


def lum_of(rgb: list[float]) -> float:
    return max(rgb)


# --- tone -----------------------------------------------------------------------------------


def test_linear_curve_passes_brightness_through():
    for v in (0.1, 0.25, 0.5, 1.0):
        assert lum_of(tone([v, v, v], preset())) == pytest.approx(v)


@pytest.mark.parametrize("gain", [TUNABLES["gain"][0], 2.0, TUNABLES["gain"][1]])
def test_gain_multiplies_and_saturates(gain):
    v = 0.2
    assert lum_of(tone([v, v, v], preset(gain=gain))) == pytest.approx(min(1.0, v * gain))
    assert lum_of(tone([1.0, 1.0, 1.0], preset(gain=gain))) == pytest.approx(1.0)


@pytest.mark.parametrize("curve", [TUNABLES["curve"][0], 0.7, TUNABLES["curve"][1]])
def test_curve_is_an_exponent(curve):
    v = 0.3
    assert lum_of(tone([v, v, v], preset(curve=curve))) == pytest.approx(v**curve)
    # curve < 1 lifts dark scenes, never darkens them
    assert lum_of(tone([v, v, v], preset(curve=curve))) >= v - 1e-9


@pytest.mark.parametrize("lo,hi", [(0, 100), (TUNABLES["min_brightness"][1], 100), (5, TUNABLES["brightness"][0]), (30, 30)])
def test_output_spans_min_to_max(lo, hi):
    s = preset(min_brightness=lo, brightness=hi, gain=TUNABLES["gain"][1])
    top = lum_of(tone([1.0, 0.0, 0.0], s))
    bottom = lum_of(tone([DARK_LEVEL, DARK_LEVEL, DARK_LEVEL], preset(min_brightness=lo, brightness=hi, gain=1.0)))
    assert top == pytest.approx(hi / 100)
    assert bottom >= min(lo, hi) / 100 - 1e-9
    assert bottom <= hi / 100 + 1e-9


def test_full_color_above_chroma_level_is_kept():
    rgb = [0.8, 0.4, 0.2]
    out = tone(rgb, preset())
    assert out == pytest.approx(rgb)
    assert lum_of(rgb) > CHROMA_FULL_LEVEL


def test_noise_level_color_fades_to_warm_white():
    out = tone([DARK_LEVEL, 0.0, 0.0], preset())
    lum = lum_of(out)
    # far below CHROMA_FULL_LEVEL the hue is mostly warm white, not pure red
    assert out[1] / lum > 0.5 * _WARM_WHITE[1]


@pytest.mark.parametrize("level", sorted(DARK_CONTENT_LEVELS))
def test_black_gives_dark_content_glow(level):
    out = tone([0.0, 0.0, 0.0], preset(dark_content=level, brightness=80))
    assert lum_of(out) == pytest.approx(DARK_CONTENT_LEVELS[level] / 100 * 0.8)


def test_dark_ramp_is_continuous_at_threshold():
    s = preset(dark_content="high", min_brightness=5, gain=2.0, curve=0.7)
    below = lum_of(tone([DARK_LEVEL - 1e-6] * 3, s))
    at = lum_of(tone([DARK_LEVEL] * 3, s))
    assert below == pytest.approx(at, abs=1e-3)


def test_tv_style_off_is_black():
    s = preset(source="tv_style", tv={"category": "off"})
    assert tone([0.5, 0.5, 0.5], s) == [0.0, 0.0, 0.0]


def test_tv_style_color_is_warm_white_scaled():
    s = preset(source="tv_style", brightness=50, tv={"category": "color", "brightness_factor": 0.5})
    assert tone([0.5, 0.1, 0.1], s) == pytest.approx([c * 0.25 for c in _WARM_WHITE])


def test_tv_style_uses_tv_gain_and_curve():
    tv = {"category": "video", "brightness_factor": 1.0, "gain": 2.0, "curve": 1.0, "saturation": 1.0}
    s = preset(source="tv_style", gain=4.0, curve=0.4, tv=tv)
    assert lum_of(tone([0.3, 0.3, 0.3], s)) == pytest.approx(0.6)


# --- smoothing ------------------------------------------------------------------------------


def test_smoother_converges_to_target():
    sm = _Smoother(2)
    targets = [[0.8, 0.2, 0.1], [0.0, 0.5, 0.5]]
    out = None
    for _ in range(500):
        out = sm.step(targets, 0.02, 0.15, 0.3)
    for o, t in zip(out, targets):
        assert o == pytest.approx(t, abs=1e-4)


def test_smoother_moves_monotonically_and_slower_with_longer_tau():
    fast, slow = _Smoother(1), _Smoother(1)
    target = [[0.6, 0.6, 0.6]]
    prev = 0.0
    for _ in range(10):
        f = fast.step(target, 0.02, 0.05, 0.05)[0][0]
        s = slow.step(target, 0.02, 1.0, 1.0)[0][0]
        assert prev <= f <= 0.6
        assert s < f
        prev = f


def test_smoother_resizes_on_channel_change():
    sm = _Smoother(1)
    out = sm.step([[0.1, 0.1, 0.1], [0.2, 0.2, 0.2]], 0.02, 0.1, 0.1)
    assert len(out) == 2


def test_native_filter_converges_to_grey_target():
    nf = _NativeFilter(1)
    nf.step([[0.0, 0.0, 0.0]], 0.02, 2.0)
    out = None
    for _ in range(1000):
        out = nf.step([[0.3, 0.3, 0.3]], 0.02, 2.0)
    assert out[0] == pytest.approx([0.3, 0.3, 0.3], abs=1e-3)


def test_ramp_without_delay_passes_through():
    r = _Ramp(2)
    targets = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    assert r.step(0.0, targets, 0.0) == targets


def test_ramp_with_delay_reaches_target():
    r = _Ramp(1)
    r.step(0.0, [[0.0, 0.0, 0.0]], 1.0)
    out = None
    t = 0.0
    while t < 5.0:
        t += 0.02
        out = r.step(t, [[0.5, 0.5, 0.5]], 1.0)
    assert out[0] == pytest.approx([0.5, 0.5, 0.5])
