"""TvReader (tv_reader.py) against a scripted fake session and a fake clock; no network."""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest
import requests

from helpers import uniform_layer
from halo_sync import tv_reader as tv_reader_mod
from halo_sync.const import (
    TV_BACKOFF_MAX_S,
    TV_BLACK_STANDBY_S,
    TV_MENU_STYLE,
    TV_POWER_POLL_S,
    TV_STANDBY_POLL_S,
    TV_STYLE_POLL_S,
)
from halo_sync.tv_reader import TvReader, is_black

SIZES = {"left": 4, "top": 8, "right": 4, "bottom": 0}  # the target TV, no bottom side
COLOR = uniform_layer(SIZES, (120, 60, 30))
BLACK = uniform_layer(SIZES, (0, 0, 0))


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Resp:
    def __init__(self, data) -> None:
        self._data = data

    def json(self):
        return self._data


class _FakeSession:
    """Answers like JointSpace v6; `cost` is how long each request takes on the fake clock."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.auth = self.verify = None
        self.calls: list[str] = []
        self.power = "On"
        self.layer = COLOR
        self.fail = False
        self.cost = 0.0
        self.closed = False

    def _answer(self, url: str):
        self.calls.append(url)
        self.clock.t += self.cost
        if self.fail:
            raise requests.ConnectionError("TV is off")
        if url.endswith("/powerstate"):
            return _Resp({"powerstate": self.power})
        if url.endswith("/ambilight/currentconfiguration"):
            return _Resp({"styleName": "FOLLOW_VIDEO", "isExpert": False, "menuSetting": "STANDARD"})
        if url.endswith("/menuitems/settings/current"):
            return _Resp({"values": [{"value": {"Nodeid": TV_MENU_STYLE, "data": {"activenode_id": 1}}}]})
        if re.search(r"/ambilight/(measured|processed)$", url):
            return _Resp({"layer1": self.layer})
        raise AssertionError(f"unexpected request {url}")

    def get(self, url, timeout=None):
        return self._answer(url)

    def post(self, url, json=None, timeout=None):
        return self._answer(url)

    def close(self) -> None:
        self.closed = True

    def count(self, suffix: str) -> int:
        return sum(1 for u in self.calls if u.endswith(suffix))


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def session(clock) -> _FakeSession:
    return _FakeSession(clock)


@pytest.fixture
def changes() -> list[str]:
    return []


@pytest.fixture
def reader(clock, session, changes) -> TvReader:
    r = TvReader("tv.invalid", "user", "pass", session_factory=lambda: session, clock=clock)
    r._on_change = lambda: changes.append(r.state)
    return r


def _wait_and_step(reader: TvReader, clock: _Clock, delay: float) -> float:
    clock.t += delay
    return reader._step()


# ---- session -------------------------------------------------------------
def test_session_uses_digest_auth_and_skips_tls_verification(reader, session):
    assert isinstance(session.auth, requests.auth.HTTPDigestAuth)
    assert session.verify is False


def test_exactly_one_session_for_the_whole_run():
    created: list[_FakeSession] = []
    clock = _Clock()
    enough = threading.Event()

    def factory():
        s = _FakeSession(clock)
        created.append(s)
        return s

    r = TvReader("tv.invalid", "u", "p", hz=200, session_factory=factory)
    orig = created[0]._answer

    def answer(url):
        resp = orig(url)
        if r.read_count >= 5:
            enough.set()
        return resp

    created[0]._answer = answer
    r.start()
    try:
        assert enough.wait(5), "reader did not poll"
    finally:
        r.stop()
    assert len(created) == 1
    assert created[0].closed
    assert r._thread is not None and not r._thread.is_alive()


def test_polls_the_configured_endpoint(clock, session):
    r = TvReader("tv.invalid", "u", "p", endpoint="processed", session_factory=lambda: session, clock=clock)
    r._step()
    assert session.count("/ambilight/processed") == 1
    assert session.count("/ambilight/measured") == 0


def test_unknown_endpoint_falls_back_to_measured(clock, session):
    r = TvReader("tv.invalid", "u", "p", endpoint="bogus", session_factory=lambda: session, clock=clock)
    r._step()
    assert session.count("/ambilight/measured") == 1


# ---- rate ----------------------------------------------------------------
def test_waits_the_rest_of_the_period(reader, session):
    session.cost = 0.03
    reader._step()  # first cycle also checks /powerstate
    delay = reader._step()
    assert delay == pytest.approx(0.1 - 0.03)


def test_slow_answer_is_not_overlapped_and_not_caught_up(reader, session, clock):
    session.cost = 0.25  # longer than the 100 ms period
    reader._step()
    before = len(session.calls)
    assert reader._step() == 0.0
    assert len(session.calls) == before + 1  # one request per cycle, nothing parallel or queued


def test_ten_reads_per_second(reader, session, clock):
    session.cost = 0.02
    start = clock.t
    delay = 0.0
    while clock.t + delay < start + 1.0:
        delay = _wait_and_step(reader, clock, delay)
    assert reader.read_count == 10


def test_latest_values_and_timestamp(reader, clock):
    assert reader.latest_measured is None and reader.latest_at is None
    reader._step()
    assert reader.latest_measured == COLOR
    assert reader.latest_at == clock.t
    assert reader.state == "ok"
    assert len(reader.read_times_ms()) == 1


# ---- backoff ---------------------------------------------------------------
def test_backoff_doubles_up_to_the_maximum(reader, session):
    session.fail = True
    delays = [reader._step() for _ in range(9)]
    assert delays == [0.5, 1, 2, 4, 8, 16, TV_BACKOFF_MAX_S, TV_BACKOFF_MAX_S, TV_BACKOFF_MAX_S]
    assert reader.state == "unreachable"


def test_backoff_resets_after_success(reader, session, changes):
    session.fail = True
    for _ in range(4):
        reader._step()
    session.fail = False
    assert reader._step() < 0.5
    assert reader.state == "ok"
    session.fail = True
    assert reader._step() == 0.5
    assert changes == ["ok", "unreachable"]


def test_bad_json_counts_as_failure(reader, session):
    session.layer = None  # {"layer1": null}
    assert reader._step() == 0.5
    assert reader.state == "unreachable"


# ---- standby ---------------------------------------------------------------
def test_powerstate_standby(reader, session, clock, changes):
    reader._step()
    assert reader.state == "ok"
    session.power = "Standby"
    clock.t += TV_POWER_POLL_S
    reads = reader.read_count
    assert reader._step() == TV_STANDBY_POLL_S
    assert reader.state == "standby"
    assert reader.read_count == reads  # no color read once the TV reports standby
    # Still asleep: only /powerstate, every TV_STANDBY_POLL_S.
    calls = len(session.calls)
    assert _wait_and_step(reader, clock, TV_STANDBY_POLL_S) == TV_STANDBY_POLL_S
    assert session.calls[calls:] == ["https://tv.invalid:1926/6/powerstate"]
    session.power = "On"
    assert _wait_and_step(reader, clock, TV_STANDBY_POLL_S) == 0.0
    assert reader.state == "ok"
    assert changes == ["ok", "standby", "ok"]


def test_powerstate_is_checked_only_every_few_seconds(reader, session, clock):
    delay = 0.0
    for _ in range(30):  # 3 s at 10 Hz
        delay = _wait_and_step(reader, clock, delay)
    assert session.count("/powerstate") == 1
    clock.t += TV_POWER_POLL_S
    reader._step()
    assert session.count("/powerstate") == 2


def test_black_edge_for_five_seconds_is_standby(reader, session, clock, changes):
    session.layer = BLACK
    delay, start = 0.0, clock.t
    while reader.state != "standby" and clock.t < start + 10:
        delay = _wait_and_step(reader, clock, delay)
    assert reader.state == "standby"
    assert clock.t - start == pytest.approx(TV_BLACK_STANDBY_S, abs=0.11)
    assert delay == TV_STANDBY_POLL_S
    # Picture comes back: leaves standby on the next slow poll.
    session.layer = COLOR
    assert _wait_and_step(reader, clock, TV_STANDBY_POLL_S) == 0.0
    assert reader.state == "ok"
    assert changes == ["ok", "standby", "ok"]


def test_black_stays_standby_while_power_is_on(reader, session, clock):
    session.layer = BLACK
    reader._step()
    clock.t += TV_BLACK_STANDBY_S
    reader._step()
    assert reader.state == "standby"
    assert _wait_and_step(reader, clock, TV_STANDBY_POLL_S) == TV_STANDBY_POLL_S
    assert reader.state == "standby"


def test_short_black_scene_is_not_standby(reader, session, clock):
    session.layer = BLACK
    reader._step()
    clock.t += TV_BLACK_STANDBY_S - 1
    reader._step()
    session.layer = COLOR
    reader._step()  # resets the black timer
    session.layer = BLACK
    clock.t += 2
    reader._step()
    assert reader.state == "ok"


def test_unreachable_while_in_standby(reader, session, clock):
    session.power = "Standby"
    reader._step()
    assert reader.state == "standby"
    session.fail = True
    assert _wait_and_step(reader, clock, TV_STANDBY_POLL_S) == 0.5
    assert reader.state == "unreachable"


def test_unreadable_powerstate_counts_as_on(reader, session):
    session.power = None
    reader._step()
    assert reader.state == "ok"


# ---- callbacks -------------------------------------------------------------
def test_on_change_only_on_state_changes(reader, clock, changes):
    delay = 0.0
    for _ in range(50):
        delay = _wait_and_step(reader, clock, delay)
    assert changes == ["ok"]


def test_failing_callback_does_not_break_the_reader(clock, session):
    def boom():
        raise RuntimeError("callback")

    r = TvReader("tv.invalid", "u", "p", on_change=boom, session_factory=lambda: session, clock=clock)
    r._step()
    assert r.state == "ok"


# ---- style -----------------------------------------------------------------
def test_style_is_polled_only_when_wanted_and_rate_limited(reader, session, clock):
    delay = 0.0
    for _ in range(10):
        delay = _wait_and_step(reader, clock, delay)
    assert session.count("/currentconfiguration") == 0
    assert reader.style is None and reader.menu_settings is None
    reader.style_wanted = True
    for _ in range(int(TV_STYLE_POLL_S * 10)):  # TV_STYLE_POLL_S at 10 Hz
        delay = _wait_and_step(reader, clock, delay)
    assert session.count("/currentconfiguration") == 1
    assert session.count("/menuitems/settings/current") == 1
    assert reader.style["styleName"] == "FOLLOW_VIDEO"
    assert reader.menu_settings == {TV_MENU_STYLE: {"activenode_id": 1}}
    clock.t += TV_STYLE_POLL_S
    reader._step()
    assert session.count("/currentconfiguration") == 2


# ---- helpers and boundaries ------------------------------------------------
def test_is_black():
    assert is_black(BLACK)
    assert is_black({})
    assert not is_black(COLOR)
    almost = uniform_layer(SIZES, (0, 0, 0))
    almost["top"]["3"] = {"r": 0, "g": 1, "b": 0}
    assert not is_black(almost)


def test_reader_does_not_import_home_assistant():
    source = Path(tv_reader_mod.__file__).read_text()
    assert "homeassistant" not in source
    assert "hass" not in re.sub(r"#.*", "", source)
