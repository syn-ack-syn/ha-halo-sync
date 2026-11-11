"""The one connection to the TV's JointSpace API (runs in its own thread).

Stream, color sensor and TV style all read from here. JointSpace hangs when it gets new TLS
handshakes at a high rate, so everything goes through a single keep-alive requests.Session at a
fixed rate, with a growing pause after errors and a slow poll while the TV is in standby.

This module must not import Home Assistant: state changes are reported through `on_change`, which
the caller hands over to its event loop.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Literal

import requests
from requests.auth import HTTPDigestAuth

from .const import (
    TV_BACKOFF_MAX_S,
    TV_BACKOFF_MIN_S,
    TV_BLACK_STANDBY_S,
    TV_ENDPOINT,
    TV_ENDPOINTS,
    TV_MENU_BRIGHTNESS,
    TV_MENU_SATURATION,
    TV_MENU_STYLE,
    TV_MENU_VIDEO_STYLE,
    TV_POLL_HZ,
    TV_POWER_POLL_S,
    TV_REQUEST_TIMEOUT_S,
    TV_STANDBY_POLL_S,
    TV_STYLE_POLL_S,
)

_LOGGER = logging.getLogger(__name__)

State = Literal["ok", "standby", "unreachable"]
MENU_NODES = (TV_MENU_STYLE, TV_MENU_VIDEO_STYLE, TV_MENU_BRIGHTNESS, TV_MENU_SATURATION)


def is_black(layer: dict) -> bool:
    """True when every pixel of a JointSpace layer is (0, 0, 0), or the layer has no pixels."""
    return all(
        not (px.get("r") or px.get("g") or px.get("b"))
        for side in layer.values()
        for px in side.values()
    )


class TvReader:
    """Polls the Ambilight colors (and, on demand, the TV's Ambilight style) over one session."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        *,
        hz: float = TV_POLL_HZ,
        on_change: Callable[[], None] | None = None,
        endpoint: str = TV_ENDPOINT,
        session_factory: Callable[[], requests.Session] = requests.Session,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._base = f"https://{host}:1926/6"
        self._endpoint = endpoint if endpoint in TV_ENDPOINTS else TV_ENDPOINT
        self._period = 1 / hz
        self._on_change = on_change
        self._clock = clock
        self._session = session_factory()
        self._session.auth = HTTPDigestAuth(user, password)
        self._session.verify = False  # the TV uses a self-signed certificate
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._state: State = "unreachable"  # nothing read yet
        self._latest: dict | None = None
        self._latest_at: float | None = None
        self._style: dict | None = None
        self._menu: dict | None = None
        self._backoff = TV_BACKOFF_MIN_S
        self._black_since: float | None = None
        self._power_due = 0.0
        self._style_due = 0.0
        # Set by the consumer of the TV style; the style endpoints are only polled while it is True.
        self.style_wanted = False
        self.read_count = 0  # successful color reads, for the stream statistics
        self._read_ms: deque[float] = deque(maxlen=100)

    # ---- public ----------------------------------------------------------
    @property
    def latest_measured(self) -> dict | None:
        """The last `layer1` read from the TV (from /measured or /processed, see TV_ENDPOINT)."""
        return self._latest

    @property
    def latest_at(self) -> float | None:
        """time.monotonic() of the last successful color read."""
        return self._latest_at

    @property
    def state(self) -> State:
        return self._state

    @property
    def style(self) -> dict | None:
        """/6/ambilight/currentconfiguration, polled at most every TV_STYLE_POLL_S while style_wanted."""
        return self._style

    @property
    def menu_settings(self) -> dict | None:
        """Ambilight settings menu nodes (node id -> data), polled together with `style`."""
        return self._menu

    def read_times_ms(self) -> list[float]:
        """Response times of the most recent color reads."""
        return list(self._read_ms)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="halo_sync_tv", daemon=True)
        self._thread.start()

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        thread = self._thread
        if wait and thread and thread is not threading.current_thread():
            thread.join(timeout=TV_REQUEST_TIMEOUT_S + 1)
        self._session.close()

    # ---- loop ------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                delay = self._step()
            except Exception:  # noqa: BLE001 - the reader must keep running
                _LOGGER.exception("TV reader step failed")
                delay = self._fail()
            if delay > 0:
                self._stop.wait(delay)

    def _step(self) -> float:
        """One poll cycle; returns how long to wait before the next one.

        Requests run one after another in this thread, so a slow answer never overlaps the next
        request; a cycle that took longer than the period is followed by the next one at once.
        """
        t0 = self._clock()
        try:
            if self._state == "standby":
                return self._step_standby(t0)
            if t0 >= self._power_due:
                self._power_due = t0 + TV_POWER_POLL_S
                if self._power_off():
                    self._set_state("standby")
                    return TV_STANDBY_POLL_S
            layer = self._read_colors()
            self._poll_style(t0)
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
            _LOGGER.debug("TV read failed: %s", exc)
            return self._fail()
        self._backoff = TV_BACKOFF_MIN_S
        now = self._clock()
        if is_black(layer):
            if self._black_since is None:
                self._black_since = now
            elif now - self._black_since >= TV_BLACK_STANDBY_S:
                self._set_state("standby")
                return TV_STANDBY_POLL_S
        else:
            self._black_since = None
        self._set_state("ok")
        return max(0.0, self._period - (now - t0))

    def _step_standby(self, t0: float) -> float:
        """Standby: check every TV_STANDBY_POLL_S whether the TV is on and shows something again."""
        if not self._power_off():
            layer = self._read_colors()
            if not is_black(layer):
                self._backoff = TV_BACKOFF_MIN_S
                self._black_since = None
                self._power_due = t0 + TV_POWER_POLL_S
                self._set_state("ok")
                return 0.0
        self._backoff = TV_BACKOFF_MIN_S
        return TV_STANDBY_POLL_S

    def _fail(self) -> float:
        delay = self._backoff
        self._backoff = min(TV_BACKOFF_MAX_S, self._backoff * 2)
        self._black_since = None
        self._set_state("unreachable")
        return delay

    def _set_state(self, state: State) -> None:
        if state == self._state:
            return
        _LOGGER.debug("TV reader: %s -> %s", self._state, state)
        self._state = state
        if self._on_change:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001 - a broken callback must not stop the reader
                _LOGGER.exception("TV reader on_change failed")

    # ---- requests --------------------------------------------------------
    def _read_colors(self) -> dict:
        t = time.perf_counter()
        layer = self._session.get(f"{self._base}/ambilight/{self._endpoint}", timeout=TV_REQUEST_TIMEOUT_S).json()["layer1"]
        if not isinstance(layer, dict):
            raise ValueError("unexpected Ambilight layer")
        self._read_ms.append((time.perf_counter() - t) * 1000)
        self.read_count += 1
        self._latest, self._latest_at = layer, self._clock()
        return layer

    def _power_off(self) -> bool:
        """True when /powerstate reports something other than On; an unreadable answer counts as on."""
        resp = self._session.get(f"{self._base}/powerstate", timeout=TV_REQUEST_TIMEOUT_S)
        try:
            power = resp.json().get("powerstate")
        except (ValueError, AttributeError):
            return False
        return power is not None and power != "On"

    def _poll_style(self, now: float) -> None:
        if not self.style_wanted or now < self._style_due:
            return
        self._style_due = now + TV_STYLE_POLL_S
        try:
            self._style = self._session.get(
                f"{self._base}/ambilight/currentconfiguration", timeout=TV_REQUEST_TIMEOUT_S
            ).json()
        except ValueError:
            pass  # keep the previous value
        try:
            values = self._session.post(
                f"{self._base}/menuitems/settings/current",
                json={"nodes": [{"nodeid": n} for n in MENU_NODES]},
                timeout=TV_REQUEST_TIMEOUT_S,
            ).json()["values"]
            self._menu = {v["value"]["Nodeid"]: v["value"].get("data") or {} for v in values}
        except (ValueError, KeyError, TypeError):
            pass
