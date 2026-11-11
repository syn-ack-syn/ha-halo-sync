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
# Shaped like the 2018 55PUS7373: model, serial number and software version only encrypted.
SYSTEM = {
    "api_version": {"Major": 6, "Minor": 4, "Patch": 0},
    "model_encrypted": "bW9kZWw=",
    "serialnumber_encrypted": "c2VjcmV0",
    "softwareversion_encrypted": "c29mdHdhcmU=",
    "name": "Living room TV",
    "deviceid_encrypted": "ZGV2aWNl",
    "country": "Germany",
}
SECRETS = {"HALO_TV_HOST": "tv.example", "HALO_TV_USER": "pairing-user-1234", "HALO_TV_PASS": "pairing-pass-5678"}


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
    def __init__(self, body, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class FakeSession:
    """Answers like the TV. `latency` maps a path to seconds; `fail` counts failures per path."""

    def __init__(
        self, clock: FakeClock, *, latency=None, fail=None, frame_every=0.2, power="On", bodies=None, status=None
    ) -> None:
        self.clock = clock
        self.latency = latency or {}
        self.fail = dict(fail or {})
        self.frame_every = frame_every
        self.power = power
        self.bodies = bodies or {}
        self.status = status or {}
        self.calls: list[str] = []
        self.starts: list[float] = []
        self.in_flight = 0
        self.closed = False

    def get(self, url: str, timeout: float):
        assert self.in_flight == 0, "parallel request"
        self.in_flight += 1
        try:
            path = url.split(":1926", 1)[1]
            self.calls.append(path)
            self.starts.append(self.clock.t)
            self.clock.t += self.latency.get(path, 0.05)
            if self.fail.get(path, 0) > 0:
                self.fail[path] -= 1
                raise ConnectionError("timeout")
            if path in self.status:
                return FakeResponse(None, self.status[path])
            return FakeResponse(self._body(path))
        finally:
            self.in_flight -= 1

    def _body(self, path: str):
        if path in self.bodies:
            return self.bodies[path]
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
    kw.setdefault("secrets", list(SECRETS.values()))
    return cf.capture(cap, tmp_path, today="2026-11-11", **kw)


def test_anonymize_system_keeps_only_api_version_and_model():
    assert cf.anonymize_system(SYSTEM) == {"api_version": SYSTEM["api_version"]}
    assert cf.anonymize_system({"api_version": 6, "model": "55PUS7373/12", "serialnumber": "x"}) == {
        "api_version": 6,
        "model": "55PUS7373/12",
    }
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
    assert cf.check_no_leaks(tmp_path, list(SECRETS.values())) == []
    for f in tmp_path.iterdir():
        text = f.read_text().lower()
        assert "serial" not in text and "living room" not in text and "encrypted" not in text


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
    (tmp_path / "d.json").write_text('{"left": 4, "version": "6.4.0", "t": "12:30:45"}')
    (tmp_path / "e.json").write_text('{"x": "1C:5A:6B:00:11:2f"}')
    (tmp_path / "f.json").write_text('{"x": "1c-5a-6b-00-11-2f"}')
    assert cf.check_no_leaks(tmp_path, ["my-secret-host"]) == ["a.json", "b.json", "c.json", "e.json", "f.json"]


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


def _run_main(monkeypatch, out, argv=(), **session_kw):
    """main() with the fake session and clock; log output goes to stderr as in a real run."""
    for var, value in SECRETS.items():
        monkeypatch.setenv(var, value)
    clock = FakeClock()
    sessions = []

    def factory(user, password):
        assert (user, password) == (SECRETS["HALO_TV_USER"], SECRETS["HALO_TV_PASS"])
        s = FakeSession(clock, **session_kw)
        sessions.append(s)
        return s

    real_capture = cf.Capture

    def fake_capture(session, base_url, **kw):
        assert base_url == "https://tv.example:1926"
        return real_capture(session, base_url, clock=clock, sleep=clock.sleep)

    monkeypatch.setattr(cf, "Capture", fake_capture)
    code = cf.main(["--out", str(out), "--dauer", "1", *argv], session_factory=factory)
    return code, sessions


def test_main_uses_one_session_and_closes_it(monkeypatch, tmp_path):
    code, sessions = _run_main(monkeypatch, tmp_path / "out")
    assert code == 0
    assert len(sessions) == 1 and sessions[0].closed
    assert (tmp_path / "out" / "timing.json").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]  # no temporary directories left


def test_requests_start_at_least_100_ms_apart_across_endpoints(monkeypatch, tmp_path):
    code, (session,) = _run_main(monkeypatch, tmp_path / "out", latency={"/6/system": 0.0, "/6/powerstate": 0.0})
    assert code == 0
    gaps = [b - a for a, b in zip(session.starts, session.starts[1:])]
    assert min(gaps) >= cf.MIN_GAP_S - 1e-9


def test_401_fails_at_once_without_retry(monkeypatch, tmp_path, capsys):
    code, (session,) = _run_main(monkeypatch, tmp_path / "out", status={"/6/system": 401})
    assert code != 0
    assert session.calls == ["/6/system"]
    assert "authentication failed" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_401_while_sampling_also_stops(tmp_path):
    clock = FakeClock()
    session = FakeSession(clock, status={"/6/ambilight/measured": 401})
    with pytest.raises(cf.CaptureError, match="authentication failed"):
        _capture(clock, session, tmp_path / "out")
    assert session.calls.count("/6/ambilight/measured") == 1


def test_leak_discards_everything_and_keeps_old_output(monkeypatch, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.json").write_text("{}")
    code, _ = _run_main(monkeypatch, out, bodies={"/6/ambilight/topology": {"left": 4, "mac": "1c:5a:6b:00:11:2f"}})
    assert code != 0
    assert sorted(p.name for p in out.iterdir()) == ["keep.json"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]


def test_success_replaces_old_output(monkeypatch, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "stale.json").write_text("{}")
    code, _ = _run_main(monkeypatch, out)
    assert code == 0
    assert not (out / "stale.json").exists()
    assert (out / "topology.json").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]


FAILURES = {
    "ok": {},
    "auth": {"status": {"/6/system": 401}},
    "unreachable": {"fail": {"/6/system": 99}},
    "server_error": {"status": {"/6/powerstate": 500}},
    "standby": {"power": "Standby"},
    "leak_secret": {"bodies": {"/6/ambilight/topology": {"name": "tv.example pairing-user-1234"}}},
    "leak_ip": {"bodies": {"/6/ambilight/topology": {"ip": "192.168.1.20"}}},
}


@pytest.mark.parametrize("case", sorted(FAILURES))
def test_host_and_credentials_never_printed(case, monkeypatch, tmp_path, capsys):
    _run_main(monkeypatch, tmp_path / "out", **FAILURES[case])
    captured = capsys.readouterr()
    for value in SECRETS.values():
        assert value not in captured.out and value not in captured.err
