"""tools/capture_fixture.py against a fake session and a fake clock: no network, no real time."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from helpers import uniform_layer

TOOL = Path(__file__).resolve().parents[1] / "tools" / "capture_fixture.py"
_spec = importlib.util.spec_from_file_location("capture_fixture", TOOL)
cf = importlib.util.module_from_spec(_spec)
sys.modules["capture_fixture"] = cf
_spec.loader.exec_module(cf)

SIZES = {"left": 4, "top": 8, "right": 4, "bottom": 0}
SYSTEM = {
    "api_version": {"Major": 6, "Minor": 4, "Patch": 0},
    "model": "55PUS7373/12",
    "serialnumber_encrypted": "c2VjcmV0",
    "name": "Living room TV",
    "deviceid_encrypted": "ZGV2aWNl",
    "softwareversion": "TPM181E_R.101.001.208.001",
    "country": "Germany",
}


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        assert s >= 0
        self.sleeps.append(s)
        self.t += s


class FakeResponse:
    def __init__(self, body) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._body


class FakeSession:
    """Answers like the TV. `latency` maps a path to seconds; `fail` counts failures per path."""

    def __init__(self, clock: FakeClock, *, latency=None, fail=None, frame_every=0.2, power="On") -> None:
        self.clock = clock
        self.latency = latency or {}
        self.fail = dict(fail or {})
        self.frame_every = frame_every
        self.power = power
        self.calls: list[str] = []
        self.in_flight = 0
        self.closed = False

    def get(self, url: str, timeout: float):
        assert self.in_flight == 0, "parallel request"
        self.in_flight += 1
        try:
            path = url.split(":1926", 1)[1]
            self.calls.append(path)
            self.clock.t += self.latency.get(path, 0.05)
            if self.fail.get(path, 0) > 0:
                self.fail[path] -= 1
                raise ConnectionError("timeout")
            return FakeResponse(self._body(path))
        finally:
            self.in_flight -= 1

    def _body(self, path: str):
        if path == "/6/system":
            return SYSTEM
        if path == "/6/powerstate":
            return {"powerstate": self.power}
        if path == "/6/ambilight/topology":
            return {"layers": "1", "left": 4, "top": 8, "bottom": 0, "right": 4}
        frame = int(self.clock.t / self.frame_every) % 256
        return {"layer1": uniform_layer(SIZES, (frame, 10, 20))}

    def close(self) -> None:
        self.closed = True


def _capture(clock, session, tmp_path, **kw):
    cap = cf.Capture(session, "https://tv.example:1926", clock=clock, sleep=clock.sleep, log=lambda m: None)
    kw.setdefault("dauer", 3)
    kw.setdefault("hz", 10)
    kw.setdefault("secrets", ["tv.example", "pairing-user-1234", "pairing-pass-5678"])
    return cf.capture(cap, tmp_path, today="2026-11-11", **kw)


def test_anonymize_system_keeps_only_api_version_and_model():
    assert cf.anonymize_system(SYSTEM) == {"api_version": SYSTEM["api_version"], "model": "55PUS7373/12"}
    assert cf.anonymize_system({"serialnumber": "x"}) == {}


def test_capture_writes_anonymised_fixtures(tmp_path):
    clock = FakeClock()
    session = FakeSession(clock)
    _capture(clock, session, tmp_path)

    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == sorted(
        ["system.json", "powerstate_on.json", "topology.json", "timing.json"]
        + [f"{ep}_{i:02d}.json" for ep in ("measured", "processed") for i in range(20)]
    )
    assert json.loads((tmp_path / "system.json").read_text()) == cf.anonymize_system(SYSTEM)
    assert json.loads((tmp_path / "powerstate_on.json").read_text()) == {"powerstate": "On"}
    assert "layer1" in json.loads((tmp_path / "measured_00.json").read_text())
    assert cf.check_no_leaks(tmp_path, ["tv.example", "pairing-user-1234", "pairing-pass-5678"]) == []
    for f in tmp_path.iterdir():
        text = f.read_text().lower()
        assert "serial" not in text and "living room" not in text and "tpm181e" not in text


def test_capture_never_overlaps_and_respects_the_rate(tmp_path):
    clock = FakeClock()
    session = FakeSession(clock, latency={"/6/ambilight/measured": 0.03})
    timing = _capture(clock, session, tmp_path, dauer=2, hz=10)
    v = timing["video"]["measured"]
    assert v["errors"] == 0
    assert 19 <= v["requests"] <= 21  # 10 Hz for 2 s
    assert sum(v["answers_per_second"]) == v["requests"]
    assert all(9 <= n <= 11 for n in v["answers_per_second"])


def test_slow_answers_delay_instead_of_burst():
    clock = FakeClock()
    session = FakeSession(clock, latency={"/x": 0.25})
    cap = cf.Capture(session, "https://tv.example:1926", clock=clock, sleep=clock.sleep, log=lambda m: None)
    samples = cap.sample("/x", interval=0.1, dauer=1.0)
    gaps = [b.t - a.t for a, b in zip(samples, samples[1:])]
    assert gaps and all(g >= 0.25 - 1e-9 for g in gaps)


def test_errors_back_off_with_growing_pause(tmp_path):
    clock = FakeClock()
    session = FakeSession(clock, fail={"/6/system": 4})
    _capture(clock, session, tmp_path)
    assert clock.sleeps[:4] == [0.5, 1.0, 2.0, 4.0]
    assert session.calls[:5] == ["/6/system"] * 5


def test_gives_up_after_too_many_errors(tmp_path):
    clock = FakeClock()
    session = FakeSession(clock, fail={"/6/system": 99})
    with pytest.raises(cf.CaptureError, match="giving up"):
        _capture(clock, session, tmp_path)
    assert session.calls.count("/6/system") == cf.MAX_CONSECUTIVE_ERRORS
    assert max(clock.sleeps) <= cf.BACKOFF_MAX_S


def test_refuses_when_tv_is_not_on(tmp_path):
    clock = FakeClock()
    with pytest.raises(cf.CaptureError, match="not on"):
        _capture(clock, FakeSession(clock, power="Standby"), tmp_path)


def test_leak_check_catches_ip_secret_and_words(tmp_path):
    (tmp_path / "a.json").write_text('{"x": "192.168.1.20"}')
    (tmp_path / "b.json").write_text('{"x": "My-Secret-Host"}')
    (tmp_path / "c.json").write_text('{"Password": 1}')
    (tmp_path / "d.json").write_text('{"left": 4, "version": "6.4.0"}')
    assert cf.check_no_leaks(tmp_path, ["my-secret-host"]) == ["a.json", "b.json", "c.json"]


def _samples(bodies_and_times):
    return [cf.Sample(t, ms, body) for t, ms, body in bodies_and_times]


def test_summarize_counts_changes_per_second():
    s = _samples(
        [
            (0.0, 50, "a"),
            (0.1, 60, "a"),
            (0.2, 70, "b"),
            (0.5, None, None),  # error: neither an answer nor a change
            (0.9, 80, "c"),
            (1.0, 90, "c"),
            (1.5, 100, "a"),
            (2.2, 1000, "a"),
        ]
    )
    out = cf.summarize(s, 3, 10)
    assert out["requests"] == 8
    assert out["errors"] == 1
    assert out["answers_per_second"] == [4, 2, 1]
    assert out["changes_per_second"] == [2, 1, 0]
    assert out["changes_per_second_mean"] == 1.0
    assert out["distinct_responses"] == 3
    assert out["response_ms"]["min"] == 50
    assert out["response_ms"]["median"] == 80
    assert out["response_ms"]["max"] == 1000
    assert 100 < out["response_ms"]["p95"] < 1000


def test_summarize_without_answers():
    out = cf.summarize(_samples([(0.0, None, None)]), 1, 10)
    assert out["response_ms"] is None
    assert out["changes_per_second"] == [0]


def test_percentile():
    assert cf.percentile([5.0], 95) == 5.0
    assert cf.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0
    assert cf.percentile([0.0, 10.0], 95) == pytest.approx(9.5)


def test_video_timing_sees_frame_rate(tmp_path):
    clock = FakeClock()
    session = FakeSession(clock, frame_every=0.2)  # content changes 5 times per second
    timing = _capture(clock, session, tmp_path, dauer=4, hz=10)
    for ep in ("measured", "processed"):
        assert 4 <= timing["video"][ep]["changes_per_second_mean"] <= 5
    saved = json.loads((tmp_path / "timing.json").read_text())
    assert saved == json.loads(json.dumps(timing))
    assert set(saved["fixture_samples"]) == {"measured", "processed"}


def test_main_needs_environment(monkeypatch, tmp_path, capsys):
    for var in ("HALO_TV_HOST", "HALO_TV_USER", "HALO_TV_PASS"):
        monkeypatch.delenv(var, raising=False)
    assert cf.main(["--out", str(tmp_path)], session_factory=lambda u, p: pytest.fail("no session")) == 2
    assert "HALO_TV_HOST" in capsys.readouterr().err


def test_main_rejects_fast_polling():
    with pytest.raises(SystemExit):
        cf.parse_args(["--hz", "20"])


def test_main_uses_one_session_and_closes_it(monkeypatch, tmp_path):
    monkeypatch.setenv("HALO_TV_HOST", "tv.example")
    monkeypatch.setenv("HALO_TV_USER", "pairing-user-1234")
    monkeypatch.setenv("HALO_TV_PASS", "pairing-pass-5678")
    clock = FakeClock()
    sessions = []

    def factory(user, password):
        assert (user, password) == ("pairing-user-1234", "pairing-pass-5678")
        s = FakeSession(clock)
        sessions.append(s)
        return s

    real_capture = cf.Capture

    def fake_capture(session, base_url, **kw):
        assert base_url == "https://tv.example:1926"
        return real_capture(session, base_url, clock=clock, sleep=clock.sleep, log=lambda m: None)

    monkeypatch.setattr(cf, "Capture", fake_capture)
    assert cf.main(["--out", str(tmp_path), "--dauer", "1"], session_factory=factory) == 0
    assert len(sessions) == 1 and sessions[0].closed
