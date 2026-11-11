"""What the Hue lights get after a stream: the snapshot, or off when the TV ended it and the option is on."""

from __future__ import annotations

import pytest

from halo_sync.streamer import TV_OFF_REASONS, ends_with_lights_off, lights_after_stop

SNAP = {
    "light-a": {"on": {"on": True}, "dimming": {"brightness": 60.0}, "color": {"xy": {"x": 0.4, "y": 0.4}}},
    "light-b": {"on": {"on": False}},
}
ALL_OFF = {"light-a": {"on": {"on": False}}, "light-b": {"on": {"on": False}}}


def test_option_off_tv_standby_restores_snapshot():
    assert lights_after_stop(SNAP, "tv_standby", False) == SNAP


@pytest.mark.parametrize("reason", ["tv_standby", "tv_off"])
def test_option_on_tv_reason_turns_all_off(reason):
    assert lights_after_stop(SNAP, reason, True) == ALL_OFF


@pytest.mark.parametrize("reason", ["user", "error", None])
def test_option_on_other_reason_restores_snapshot(reason):
    assert lights_after_stop(SNAP, reason, True) == SNAP


def test_empty_snapshot_stays_empty():
    assert lights_after_stop({}, "tv_standby", True) == {}
    assert lights_after_stop({}, "tv_standby", False) == {}


def test_snapshot_is_not_modified():
    lights_after_stop(SNAP, "tv_off", True)
    assert SNAP["light-a"]["on"] == {"on": True}


@pytest.mark.parametrize(
    ("reason", "off_with_tv", "expected"),
    [
        ("tv_standby", True, True),
        ("tv_off", True, True),
        ("tv_standby", False, False),
        ("tv_off", False, False),
        ("user", True, False),
        ("error", True, False),
        (None, True, False),
    ],
)
def test_ends_with_lights_off(reason, off_with_tv, expected):
    assert ends_with_lights_off(reason, off_with_tv) is expected


def test_tv_off_reasons():
    assert TV_OFF_REASONS == ("tv_standby", "tv_off")
