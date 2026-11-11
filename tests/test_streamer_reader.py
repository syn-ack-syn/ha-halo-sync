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
    assert s.stop_reason == "tv_standby"
    assert not s.running


def test_stop_during_standby_counts_as_user(make_streamer):
    reader = _FakeReader(uniform_layer(SIZES, (0, 0, 0)), state="standby")
    s = make_streamer(reader)
    s.running = True
    t = threading.Thread(target=s._run, daemon=True)
    t.start()
    time.sleep(GIVEUP_S / 3)  # in standby, but not long enough to give up
    s.stop()
    t.join(5)
    assert not t.is_alive()
    assert s.stop_reason == "user"
    assert s.error is None and not s.running


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
    assert s.stop_reason == "tv_off"


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
    """Each new client takes the next entry of `script`.

    "ok", "fail_connect" (timeout), ("break_after", n) (send fails after n packets) or ("alert",
    description, authenticated) (connect raises that fatal alert).
    """

    script: list = []
    clients: list = []

    def __init__(self, *a, **k):
        self.args = a
        self.mode = _ScriptedDTLS.script.pop(0) if _ScriptedDTLS.script else "ok"
        self.sent, self.closed = 0, False
        self.sent_at: list[float] = []
        _ScriptedDTLS.clients.append(self)

    def connect(self):
        if self.mode == "fail_connect":
            raise streamer_mod.DTLSError("handshake timeout: no answer from the bridge")
        if isinstance(self.mode, tuple) and self.mode[0] == "alert":
            raise streamer_mod.DTLSAlert(2, self.mode[1], self.mode[2])

    def send(self, data):
        if isinstance(self.mode, tuple) and self.mode[0] == "break_after" and self.sent >= self.mode[1]:
            raise OSError("network is unreachable")
        self.sent += 1
        self.sent_at.append(time.monotonic())

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
    assert first.args[4] == 5.0  # the first connect keeps the default DTLS timeout
    assert failed.args[4] == third.args[4] == streamer_mod.RECONNECT_TIMEOUT_S == 2.0
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


@pytest.mark.parametrize("alerts", [
    [("alert", 20, True)],  # authenticated bad_record_mac
    [("alert", 115, True)],  # authenticated unknown_psk_identity
])
def test_authenticated_key_alert_on_reconnect_ends_the_stream(make_streamer, scripted, alerts):
    actions = scripted([("break_after", 2)] + alerts)
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
    s = make_streamer(reader)
    assert _run(s, 2)  # ended by itself instead of retrying every 30 s
    assert "bridge rejected the reconnect" in s.error
    assert len(_ScriptedDTLS.clients) == 1 + len(alerts)
    assert actions[-1] == "stop" and not s.running and reader.fast is False


@pytest.mark.parametrize("alerts", [
    [("alert", 115, False), "ok"],  # a single plain key alert may be a stray one
    [("alert", 80, True)] * 4 + ["ok"],  # internal_error: the bridge may recover
    [("alert", 115, False), ("alert", 51, False), "ok"],  # not the same alert twice
])
def test_other_alerts_on_reconnect_keep_the_backoff(make_streamer, scripted, alerts):
    scripted([("break_after", 2)] + alerts)
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
    s = make_streamer(reader)
    assert not _run(s, 2.0)
    assert s.error is None
    assert len(_ScriptedDTLS.clients) == 2 + len(alerts) - 1
    assert _ScriptedDTLS.clients[-1].sent > 0 and s.link == "connected"


def test_rejects_our_key_rules():
    A = streamer_mod.DTLSAlert
    assert streamer_mod.rejects_our_key(A(2, 115, True))
    assert streamer_mod.rejects_our_key(A(2, 51, True))
    assert not streamer_mod.rejects_our_key(A(2, 115))  # plain epoch 0: never for good
    assert not streamer_mod.rejects_our_key(A(2, 80, True))
    assert not streamer_mod.rejects_our_key(A(1, 20, True))  # warning level
    assert not streamer_mod.rejects_our_key(streamer_mod.DTLSError("x"))
    assert streamer_mod.key_alert(A(2, 20)) == 20 and streamer_mod.key_alert(A(2, 80)) is None


def test_forged_plain_key_alerts_show_an_error_but_the_stream_recovers(make_streamer, scripted, monkeypatch):
    """Two plain unknown_psk_identity alerts (anyone on the LAN can send them) never end the stream."""
    monkeypatch.setattr(streamer_mod, "KEY_SUSPECT_MAX_S", 0.3)
    caps: list = []
    real_pause = streamer_mod.next_reconnect_pause

    def pause(p, cap=None):
        caps.append(cap)
        return real_pause(p, cap)

    monkeypatch.setattr(streamer_mod, "next_reconnect_pause", pause)
    scripted([("break_after", 2), ("alert", 115, False), ("alert", 115, False), "ok"])
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
    s = make_streamer(reader)
    errors: list = []
    s._on_change = lambda: errors.append(s.error)
    assert not _run(s, 2.0)  # still on
    assert streamer_mod.KEY_SUSPECT_ERROR in errors  # shown while it looked like a wrong key
    assert caps[:2] == [None, streamer_mod.KEY_SUSPECT_MAX_S]  # the second same alert raises the cap
    assert s.error is None  # cleared by the successful reconnect
    assert _ScriptedDTLS.clients[-1].sent > 0 and s.link == "connected"


def test_single_plain_key_alert_sets_no_error(make_streamer, scripted):
    scripted([("break_after", 2), ("alert", 115, False), "ok"])
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
    s = make_streamer(reader)
    errors: list = []
    s._on_change = lambda: errors.append(s.error)
    assert not _run(s, 1.0)
    assert len(_ScriptedDTLS.clients) == 3 and _ScriptedDTLS.clients[-1].sent > 0
    assert streamer_mod.KEY_SUSPECT_ERROR not in errors and s.error is None


def test_key_suspect_backoff_goes_up_to_300_s():
    assert streamer_mod.KEY_SUSPECT_MAX_S == 300
    p, seen = 1.0, []
    for _ in range(12):
        p = streamer_mod.next_reconnect_pause(p, streamer_mod.KEY_SUSPECT_MAX_S)
        seen.append(p)
    assert max(seen) == 300 and seen[-1] == 300 and 30 < seen[5]


def test_stop_during_the_stop_request_skips_start_and_handshake(make_streamer, scripted, monkeypatch):
    actions = scripted([("break_after", 2)])
    entered, release = threading.Event(), threading.Event()

    def put(url, json=None, **k):
        actions.append(json["action"])
        if json["action"] == "stop" and not entered.is_set():
            entered.set()  # the reconnect's stop request hangs until the test released it
            release.wait(5)
        return _Resp({})

    monkeypatch.setattr(streamer_mod.requests, "put", put)
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
    s = make_streamer(reader)
    snap = {"light-1": {"on": {"on": True}}}
    restored: list[dict] = []
    s._snapshot_lights = lambda: snap
    s._restore_lights = restored.append
    s.running = True
    t = threading.Thread(target=s._run, daemon=True)
    t.start()
    assert entered.wait(3)
    s.stop()
    release.set()
    t.join(3)
    assert not t.is_alive()
    assert actions == ["start", "stop", "stop"]  # no new start after the stop; the last stop is _run's
    assert len(_ScriptedDTLS.clients) == 1  # no handshake
    assert restored and not s.running and reader.fast is False


class _FakeHttp:
    """Stands in for the monitor's requests.Session; answers pop from `answers`, the last one repeats."""

    answers: list = []
    delay = 0.0
    calls = 0

    def __init__(self):
        self.headers, self.verify = {}, True

    def get(self, url, timeout=None):
        _FakeHttp.calls += 1
        if _FakeHttp.delay:
            time.sleep(_FakeHttp.delay)
        answer = _FakeHttp.answers.pop(0) if len(_FakeHttp.answers) > 1 else _FakeHttp.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return _Resp({"data": [answer]})

    def close(self):
        pass


ACTIVE = {"status": "active", "active_streamer": {"rid": "a", "rtype": "auth_v1"}}
INACTIVE = {"status": "inactive"}


@pytest.fixture
def monitor_http(monkeypatch):
    monkeypatch.setattr(streamer_mod, "SESSION_CHECK_S", 0.05)
    monkeypatch.setattr(streamer_mod.requests, "Session", _FakeHttp)
    _FakeHttp.delay, _FakeHttp.calls = 0.0, 0

    def use(answers, delay=0.0):
        _FakeHttp.answers, _FakeHttp.delay = list(answers), delay

    return use


def test_inactive_area_triggers_a_reconnect(make_streamer, scripted, monitor_http):
    actions = scripted(["ok", "ok"])
    monitor_http([INACTIVE, ACTIVE])
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
    s = make_streamer(reader)
    links: list[str] = []
    s._on_change = lambda: links.append(s.link)
    assert not _run(s, 0.8)
    first, second = _ScriptedDTLS.clients[:2]
    assert first.closed and second.sent > 0
    assert actions[:3] == ["start", "stop", "start"]
    assert links[:2] == ["reconnecting", "connected"]
    assert s.link == "connected" and s.error is None


def test_area_that_keeps_stopping_ends_the_stream(make_streamer, scripted, monitor_http):
    scripted([])
    monitor_http([INACTIVE])
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
    s = make_streamer(reader)
    assert _run(s, 3)
    assert s.error == "entertainment area keeps stopping, another app?"
    # the first stop and two quick repeats reconnect; the third quick repeat ends the stream
    assert len(_ScriptedDTLS.clients) == 1 + streamer_mod.INACTIVE_REPEATS_MAX
    assert not s.running and reader.fast is False


def test_other_active_streamer_is_only_logged_once(make_streamer, scripted, monitor_http, caplog):
    scripted([])
    monitor_http([{"status": "active", "active_streamer": {"rid": "someone-else", "rtype": "auth_v1"}}])
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
    s = make_streamer(reader)
    assert not _run(s, 0.5)
    assert _FakeHttp.calls >= 3
    assert len(_ScriptedDTLS.clients) == 1  # no reconnect
    warnings = [r for r in caplog.records if "streamed by" in r.getMessage()]
    assert len(warnings) == 1 and "someone-else" in warnings[0].getMessage() and "a" in warnings[0].args


def test_at_most_one_monitor_thread_is_alive(make_streamer, scripted, monitor_http):
    """Status answers trickle in (1 s each, longer than stop()'s join) while the link breaks again and again."""
    # Each link lives ~8 frames (~0.16 s), long enough for its monitor to be stuck in a request.
    scripted([("break_after", 8)] * 30)
    monitor_http([ACTIVE], delay=1.0)
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
    s = make_streamer(reader)
    mon = streamer_mod._SessionMonitor
    deadline = time.monotonic() + 3
    while mon.running and time.monotonic() < deadline:  # monitors of earlier tests may still hang
        time.sleep(0.02)
    assert mon.running == 0
    mon.peak = 0
    assert not _run(s, 2.5)
    deadline = time.monotonic() + 3
    while mon.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(_ScriptedDTLS.clients) >= 4  # many reconnects
    assert _FakeHttp.calls >= 2  # monitors did run
    assert mon.running == 0 and mon.peak == 1


@pytest.mark.parametrize("answer, expected", [
    (ACTIVE, True),
    ({"status": "active"}, True),
    ({"status": "active", "active_streamer": {"rid": "other-app"}}, True),  # logged, not acted on
    (INACTIVE, False),
    ({"status": "active", "active_streamer": "garbled"}, None),
    (streamer_mod.requests.ConnectionError("bridge down"), None),
])
def test_monitor_check(answer, expected):
    _FakeHttp.answers, _FakeHttp.delay = [answer], 0.0
    m = streamer_mod._SessionMonitor("https://bridge.invalid/x", {}, "a")
    assert m.check(_FakeHttp()) is expected


def test_slow_status_request_does_not_delay_sending(make_streamer, scripted, monitor_http):
    scripted([])
    monitor_http([ACTIVE], delay=0.4)  # every status request hangs for 0.4 s
    reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
    s = make_streamer(reader)
    assert not _run(s, 1.0)
    assert _FakeHttp.calls >= 2
    at = _ScriptedDTLS.clients[0].sent_at
    gaps = [b - a for a, b in zip(at, at[1:])]
    assert len(at) > 20 and max(gaps) < 0.15  # 50 Hz loop; a 0.4 s hang would show up here


def test_close_notify_from_a_real_session_reconnects(make_streamer, scripted, monkeypatch):
    """Real DTLSClient against the fake bridge on 127.0.0.1: no send error is faked, the bridge ends the session."""
    from test_dtls import FakeBridge

    from halo_sync import dtls

    actions = scripted([])
    monkeypatch.setattr(dtls, "RETRANSMIT_S", 0.1)
    monkeypatch.setattr(streamer_mod.requests, "Session", _FakeHttp)
    _FakeHttp.answers, _FakeHttp.delay = [ACTIVE], 0.0
    bridge = FakeBridge(bytes(16), close_notify_after=5)  # cfg hue_clientkey is "00" * 16
    real = dtls.DTLSClient
    monkeypatch.setattr(streamer_mod, "DTLSClient", lambda host, port, ident, psk, *t: real("127.0.0.1", bridge.port, ident, psk, *t))
    try:
        reader = _FakeReader(uniform_layer(SIZES, (90, 90, 90)))
        s = make_streamer(reader)
        links: list[str] = []
        s._on_change = lambda: links.append(s.link)
        assert not _run(s, 1.0)
    finally:
        bridge.close()
    assert bridge.sessions >= 2  # a new handshake after the close_notify
    assert "reconnecting" in links and "connected" in links
    assert actions[:3] == ["start", "stop", "start"]
    assert s.error is None and reader.fast is False
