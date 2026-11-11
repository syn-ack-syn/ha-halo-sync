"""How Streamer._run reacts to the TV reader: standby, black frames, a silent TV, the poll rate."""

from __future__ import annotations

import threading
import time

import pytest

from helpers import uniform_layer
from halo_sync import streamer as streamer_mod

SIZES = {"left": 4, "top": 8, "right": 4, "bottom": 0}
CHANNELS = [{"channel_id": 0, "position": {"x": -1.0, "z": 0.0}}]
GIVEUP_S = 0.3  # instead of TV_GIVEUP_S, so the tests run in well under a second


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, ""

    def json(self):
        return self._data


class _FakeReader:
    def __init__(self, layer, state="ok"):
        self._layer = layer
        self.state = state
        self.fast = False
        self.woken = 0
        self.fast_seen: list[bool] = []
        self.read_count = 0
        self.fresh = True  # latest_at follows the clock, like a reader that keeps reading

    @property
    def latest_measured(self):
        self.fast_seen.append(self.fast)
        return self._layer

    @property
    def latest_at(self):
        return time.monotonic() if self.fresh else self._stale_at

    def read_times_ms(self):
        return [10.0]

    def wake(self):
        self.woken += 1


class _FakeDTLS:
    sent = 0

    def __init__(self, *a, **k):
        _FakeDTLS.sent = 0

    def connect(self):
        pass

    def send(self, data):
        _FakeDTLS.sent += 1

    def close(self):
        pass


@pytest.fixture
def make_streamer(monkeypatch):
    monkeypatch.setattr(streamer_mod, "TV_GIVEUP_S", GIVEUP_S)
    monkeypatch.setattr(streamer_mod.requests, "get", lambda *a, **k: _Resp({"data": [{"channels": CHANNELS}]}))
    monkeypatch.setattr(streamer_mod.requests, "put", lambda *a, **k: _Resp({}))
    monkeypatch.setattr(streamer_mod, "DTLSClient", _FakeDTLS)
    cfg = {
        "tv_host": "tv.invalid", "tv_user": "u", "tv_pass": "p", "hue_host": "bridge.invalid",
        "hue_user": "k", "hue_appid": "a", "hue_clientkey": "00" * 16,
        "entertainment_id": "0a1b2c3d-0000-4000-8000-000000000001",
    }
    settings = {
        "source": "video", "dark_content": "off", "brightness": 100, "min_brightness": 0, "saturation": 1.0,
        "smoothing_color": 1, "smoothing_brightness": 1, "spread": 0.8, "gain": 1.0, "curve": 1.0,
        "zone_mode": "nearest", "native_delay": 0,
    }

    def make(reader):
        s = streamer_mod.Streamer(cfg, dict(settings), on_change=lambda: None, reader=reader)
        s._snapshot_lights = lambda: {}  # no bridge
        return s

    return make


def _run(streamer, seconds):
    """Run Streamer._run in a thread; stop it after `seconds` if it is still going. True if it ended by itself."""
    streamer.running = True
    t = threading.Thread(target=streamer._run, daemon=True)
    t.start()
    t.join(seconds)
    ended = not t.is_alive()
    streamer._stop.set()
    t.join(5)
    assert not t.is_alive()
    return ended


def test_standby_ends_the_stream_cleanly(make_streamer):
    reader = _FakeReader(uniform_layer(SIZES, (0, 0, 0)), state="standby")
    s = make_streamer(reader)
    assert _run(s, GIVEUP_S * 5)
    assert s.error == "TV in standby"
    assert not s.running


def test_black_frames_keep_the_stream_running(make_streamer):
    reader = _FakeReader(uniform_layer(SIZES, (0, 0, 0)))  # powerstate On, dark scene
    s = make_streamer(reader)
    assert not _run(s, GIVEUP_S * 3)
    assert s.error is None
    assert _FakeDTLS.sent > 0


def test_silent_tv_ends_the_stream_with_an_error(make_streamer):
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)), state="unreachable")
    reader.fresh, reader._stale_at = False, time.monotonic() - 100
    s = make_streamer(reader)
    assert _run(s, GIVEUP_S * 5)
    assert s.error == "TV stopped answering"


def test_snapshot_failure_hands_the_rate_back(make_streamer):
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
    s = make_streamer(reader)

    def broken():
        raise TypeError("unexpected bridge answer")

    s._snapshot_lights = broken
    assert _run(s, 2)
    assert reader.fast is False
    assert reader.woken == 1
    assert s.error == "unexpected bridge answer"
    assert not s.running


def test_stream_sets_fast_and_wakes_the_reader(make_streamer):
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
    s = make_streamer(reader)
    _run(s, 0.2)
    assert reader.woken == 1
    assert reader.fast_seen and all(reader.fast_seen)  # full rate while the stream reads
    assert reader.fast is False  # handed back afterwards


# ---- reconnect after a broken DTLS session ---------------------------------
class _ScriptedDTLS:
    """Each new client takes the next entry of `script`: "ok", "fail_connect" or ("break_after", n)."""

    script: list = []
    clients: list = []

    def __init__(self, *a, **k):
        self.mode = _ScriptedDTLS.script.pop(0) if _ScriptedDTLS.script else "ok"
        self.sent, self.closed = 0, False
        _ScriptedDTLS.clients.append(self)

    def connect(self):
        if self.mode == "fail_connect":
            raise streamer_mod.DTLSError("handshake timeout: no answer from the bridge")

    def send(self, data):
        if isinstance(self.mode, tuple) and self.sent >= self.mode[1]:
            raise OSError("network is unreachable")
        self.sent += 1

    def close(self):
        self.closed = True


@pytest.fixture
def scripted(monkeypatch):
    monkeypatch.setattr(streamer_mod, "RECONNECT_FIRST_S", 0.05)
    monkeypatch.setattr(streamer_mod, "RECONNECT_MAX_S", 0.2)
    monkeypatch.setattr(streamer_mod, "DTLSClient", _ScriptedDTLS)
    _ScriptedDTLS.clients = []
    actions: list[str] = []
    monkeypatch.setattr(streamer_mod.requests, "put", lambda url, json=None, **k: actions.append(json["action"]) or _Resp({}))

    def use(script):
        _ScriptedDTLS.script = list(script)
        return actions

    return use


def test_next_reconnect_pause_doubles_up_to_30_s():
    pauses, p = [], streamer_mod.RECONNECT_FIRST_S
    for _ in range(7):
        pauses.append(p)
        p = streamer_mod.next_reconnect_pause(p)
    assert pauses == [1, 2, 4, 8, 16, 30, 30]


def test_broken_link_reconnects_and_keeps_full_rate(make_streamer, scripted):
    actions = scripted([("break_after", 3), "fail_connect", "ok"])
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
    s = make_streamer(reader)
    links: list[str] = []
    s._on_change = lambda: links.append(s.link)
    assert not _run(s, 1.0)  # still streaming when stopped
    first, failed, third = _ScriptedDTLS.clients[:3]
    assert first.closed and failed.closed
    assert third.sent > 0  # streaming resumed on the new session
    assert actions[:5] == ["start", "stop", "start", "stop", "start"]  # area restarted for every attempt
    assert actions[-1] == "stop"  # final stop by _run
    assert "reconnecting" in links and links[links.index("reconnecting"):].count("connected") >= 1
    assert s.link == "connected" and "hue_link" not in s.stats
    assert reader.fast_seen and all(reader.fast_seen)  # the reader stayed at full rate while reconnecting
    assert reader.fast is False and reader.woken == 1
    assert s.error is None


def test_stop_while_reconnecting_restores_the_lights(make_streamer, scripted):
    actions = scripted([("break_after", 2)] + ["fail_connect"] * 50)
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
    s = make_streamer(reader)
    snap = {"light-1": {"on": {"on": True}}}
    restored: list[dict] = []
    s._snapshot_lights = lambda: snap
    s._restore_lights = restored.append
    assert not _run(s, 0.6)
    assert len(_ScriptedDTLS.clients) >= 3  # kept trying while the stream was on
    assert s.stats.get("hue_link") == "reconnecting" and "handshake timeout" in s.stats["hue_link_error"]
    assert actions[-1] == "stop"
    assert restored and restored[0] is snap
    assert not s.running and reader.fast is False


def test_failed_first_connect_ends_the_stream(make_streamer, scripted):
    scripted(["fail_connect"])
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
    s = make_streamer(reader)
    assert _run(s, 2)
    assert "handshake timeout" in s.error
    assert len(_ScriptedDTLS.clients) == 1 and _ScriptedDTLS.clients[0].closed
    assert reader.fast is False
