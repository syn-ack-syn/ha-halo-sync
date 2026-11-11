#!/usr/bin/env python3
"""Record anonymised JointSpace v6 responses from a Philips TV as test fixtures.

Usage (TV on, Ambilight running, a video playing for the timing part):

    HALO_TV_HOST=... HALO_TV_USER=... HALO_TV_PASS=... \\
        uv run --locked python tools/capture_fixture.py [--out DIR] [--dauer 20] [--hz 10]

Writes into the output directory (default tests/fixtures/55pus7373/):

    system.json            only api_version and model from /6/system
    powerstate_on.json     /6/powerstate
    topology.json          /6/ambilight/topology
    measured_NN.json       20 samples of /6/ambilight/measured, 100 ms apart
    processed_NN.json      20 samples of /6/ambilight/processed, 100 ms apart
    timing.json            response times and change rate per endpoint

The TV API is fragile: every request goes through one requests.Session (keep-alive), requests
are never run in parallel, the rate is capped and failures back off with a growing pause.
Host, user and password are read from the environment only and never written anywhere; the
script refuses to finish if any of them shows up in an output file.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

PORT = 1926
TIMEOUT_S = 5.0
FIXTURE_SAMPLES = 20
FIXTURE_INTERVAL_S = 0.1
MAX_HZ = 10.0
BACKOFF_START_S = 0.5
BACKOFF_MAX_S = 30.0
MAX_CONSECUTIVE_ERRORS = 6
SYSTEM_KEEP = ("api_version", "model")
ENDPOINTS = ("measured", "processed")
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "55pus7373"

IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
FORBIDDEN_WORDS = ("serialnumber", "user", "password")


class CaptureError(RuntimeError):
    pass


@dataclass
class Sample:
    t: float  # seconds since the start of the run, when the request was sent
    ms: float | None  # response time, None on error
    body: Any = None


def anonymize_system(system: dict) -> dict:
    """Keep only the fields the tests need; serial number, names and ids are dropped."""
    return {k: system[k] for k in SYSTEM_KEEP if k in system}


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (p in 0..100)."""
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def response_stats(samples: list[Sample]) -> dict | None:
    ms = [s.ms for s in samples if s.ms is not None]
    if not ms:
        return None
    return {
        "min": round(min(ms), 1),
        "median": round(statistics.median(ms), 1),
        "p95": round(percentile(ms, 95), 1),
        "max": round(max(ms), 1),
    }


def summarize(samples: list[Sample], dauer: float, hz: float) -> dict:
    """Response times and how often the answer changed, per second of the run.

    A response counts as changed when its body differs from the previous successful one.
    The first successful response is not counted as a change.
    """
    seconds = max(1, math.ceil(dauer))
    changes = [0] * seconds
    answers = [0] * seconds
    prev: Any = None
    have_prev = False
    distinct: list[str] = []
    for s in samples:
        if s.ms is None:
            continue
        bucket = min(int(s.t), seconds - 1)
        answers[bucket] += 1
        if have_prev and s.body != prev:
            changes[bucket] += 1
        prev, have_prev = s.body, True
        key = json.dumps(s.body, sort_keys=True)
        if key not in distinct:
            distinct.append(key)
    ok = sum(answers)
    return {
        "requests": len(samples),
        "errors": len(samples) - ok,
        "target_hz": hz,
        "duration_s": dauer,
        "response_ms": response_stats(samples),
        "answers_per_second": answers,
        "changes_per_second": changes,
        "changes_per_second_mean": round(sum(changes) / seconds, 2),
        "distinct_responses": len(distinct),
    }


class Capture:
    def __init__(
        self,
        session: Any,
        base_url: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr),
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self._pause = BACKOFF_START_S
        self._errors = 0

    def _get(self, path: str) -> tuple[Any, float]:
        t0 = self.clock()
        resp = self.session.get(f"{self.base_url}{path}", timeout=TIMEOUT_S)
        resp.raise_for_status()
        body = resp.json()
        return body, (self.clock() - t0) * 1000

    def _failed(self, path: str, err: Exception) -> None:
        self._errors += 1
        self.log(f"{path}: {type(err).__name__}, retrying in {self._pause:g} s")
        if self._errors >= MAX_CONSECUTIVE_ERRORS:
            raise CaptureError(f"{path}: {self._errors} errors in a row, giving up (is the TV on?)")
        self.sleep(self._pause)
        self._pause = min(self._pause * 2, BACKOFF_MAX_S)

    def _ok(self) -> None:
        self._errors = 0
        self._pause = BACKOFF_START_S

    def get(self, path: str) -> Any:
        """One request, retried with a growing pause until it succeeds or we give up."""
        while True:
            try:
                body, _ = self._get(path)
            except Exception as err:  # noqa: BLE001 - any failure of the TV is retried the same way
                self._failed(path, err)
                continue
            self._ok()
            return body

    def sample(self, path: str, *, interval: float, count: int | None = None, dauer: float | None = None) -> list[Sample]:
        """Poll path sequentially every `interval` seconds, for `count` answers or `dauer` seconds.

        Never more than one request in flight. A slow answer delays the next request instead of
        causing a burst to catch up. Errors are recorded and followed by a growing pause.
        """
        out: list[Sample] = []
        start = self.clock()
        due = start
        while True:
            now = self.clock()
            if dauer is not None and now - start >= dauer:
                break
            if count is not None and sum(1 for s in out if s.ms is not None) >= count:
                break
            if due > now:
                self.sleep(due - now)
                now = self.clock()
            t = now - start
            try:
                body, ms = self._get(path)
            except Exception as err:  # noqa: BLE001
                out.append(Sample(t, None))
                self._failed(path, err)
                due = self.clock()
                continue
            self._ok()
            out.append(Sample(t, ms, body))
            due = max(due + interval, self.clock())
        return out


def check_no_leaks(out_dir: Path, secrets: list[str]) -> list[str]:
    """Names of files that contain an IP address, a forbidden word or one of the secrets."""
    bad = []
    for f in sorted(out_dir.glob("*.json")):
        text = f.read_text()
        low = text.lower()
        if (
            IP_RE.search(text)
            or any(w in low for w in FORBIDDEN_WORDS)
            or any(s and s.lower() in low for s in secrets)
        ):
            bad.append(f.name)
    return bad


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def capture(
    cap: Capture,
    out_dir: Path,
    *,
    dauer: float,
    hz: float,
    secrets: list[str],
    today: str | None = None,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    system = cap.get("/6/system")
    _write(out_dir / "system.json", anonymize_system(system))

    power = cap.get("/6/powerstate")
    if not isinstance(power, dict) or power.get("powerstate") != "On":
        raise CaptureError(f"TV is not on (powerstate {power!r}), switch it on and start a video")
    _write(out_dir / "powerstate_on.json", power)

    _write(out_dir / "topology.json", cap.get("/6/ambilight/topology"))

    fixture_timing = {}
    for ep in ENDPOINTS:
        samples = cap.sample(f"/6/ambilight/{ep}", interval=FIXTURE_INTERVAL_S, count=FIXTURE_SAMPLES)
        ok = [s for s in samples if s.ms is not None]
        for i, s in enumerate(ok):
            _write(out_dir / f"{ep}_{i:02d}.json", s.body)
        fixture_timing[ep] = {"requests": len(samples), "errors": len(samples) - len(ok), "response_ms": response_stats(samples)}

    video = {}
    for ep in ENDPOINTS:
        cap.log(f"measuring {ep} for {dauer:g} s at {hz:g} Hz, keep the video playing")
        samples = cap.sample(f"/6/ambilight/{ep}", interval=1 / hz, dauer=dauer)
        video[ep] = summarize(samples, dauer, hz)

    timing = {
        "captured": today or date.today().isoformat(),
        "note": "fixture_samples: 20 requests 100 ms apart; video: sequential runs per endpoint with a video playing",
        "fixture_samples": fixture_timing,
        "video": video,
    }
    _write(out_dir / "timing.json", timing)

    bad = check_no_leaks(out_dir, secrets)
    if bad:
        raise CaptureError(f"possible personal data in {', '.join(bad)}, check and delete before committing")
    return timing


def make_session(user: str, password: str):  # pragma: no cover - needs a real TV
    import requests
    import urllib3
    from requests.auth import HTTPDigestAuth

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.auth = HTTPDigestAuth(user, password)
    session.verify = False  # the TV uses a self-signed certificate
    return session


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output directory (default {DEFAULT_OUT})")
    p.add_argument("--dauer", type=float, default=20.0, metavar="SEKUNDEN", help="seconds per endpoint for the video timing run (default 20)")
    p.add_argument("--hz", type=float, default=10.0, help=f"request rate for the timing run, at most {MAX_HZ:g} (default 10)")
    args = p.parse_args(argv)
    if not 0 < args.hz <= MAX_HZ:
        p.error(f"--hz must be between 0 and {MAX_HZ:g}, the TV API hangs on faster polling")
    if args.dauer <= 0:
        p.error("--dauer must be positive")
    return args


def main(argv: list[str] | None = None, session_factory: Callable[[str, str], Any] = make_session) -> int:
    args = parse_args(argv)
    host = os.environ.get("HALO_TV_HOST", "")
    user = os.environ.get("HALO_TV_USER", "")
    password = os.environ.get("HALO_TV_PASS", "")
    if not (host and user and password):
        print("Set HALO_TV_HOST, HALO_TV_USER and HALO_TV_PASS.", file=sys.stderr)
        return 2
    cap = Capture(session_factory(user, password), f"https://{host}:{PORT}")
    try:
        timing = capture(cap, args.out, dauer=args.dauer, hz=args.hz, secrets=[host, user, password])
    except CaptureError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    finally:
        close = getattr(cap.session, "close", None)
        if close:
            close()
    for ep, v in timing["video"].items():
        print(f"{ep}: {v['changes_per_second_mean']} changes/s, response ms {v['response_ms']}, errors {v['errors']}")
    print(f"written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
