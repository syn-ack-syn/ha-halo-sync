"""The one connection to the TV's JointSpace API (runs in its own thread).

Stream, color sensor and TV style all read from here. JointSpace hangs when it gets new TLS
handshakes at a high rate, so everything goes through a single keep-alive requests.Session at a
fixed rate (fast while a stream needs it, slow otherwise), with a growing pause after errors and a
slow poll while the TV reports standby.

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
    TV_ENDPOINT,
    TV_ENDPOINTS,
    TV_IDLE_POLL_HZ,
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


class _Stopped(Exception):
    """Raised between two requests of a cycle once stop() was called."""


class TvReader:
    """Polls the Ambilight colors (and, on demand, the TV's Ambilight settings) over one session."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        *,
        hz: float = TV_POLL_HZ,
        idle_hz: float = TV_IDLE_POLL_HZ,
        on_change: Callable[[], None] | None = None,
        endpoint: str = TV_ENDPOINT,
        session_factory: Callable[[], requests.Session] = requests.Session,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._base = f"https://{host}:1926/6"
        self._endpoint = endpoint if endpoint in TV_ENDPOINTS else TV_ENDPOINT
        self._fast_period = 1 / hz
        self._idle_period = 1 / idle_hz
        self._on_change = on_change
        self._clock = clock
        self._session = session_factory()
        self._session.auth = HTTPDigestAuth(user, password)
        self._session.verify = False  # the TV uses a self-signed certificate
        self._stop = threading.Event()
        self._wake = threading.Event()  # interrupts the wait between two cycles
        self._thread: threading.Thread | None = None

        self._state: State = "unreachable"  # nothing read yet
        self._latest: dict | None = None
        self._latest_at: float | None = None
        self._menu: dict | None = None
        self._style_wanted = False
        self._backoff = TV_BACKOFF_MIN_S
        self._power_due = 0.0
        self._style_due = 0.0
        # Full rate while True (set by the stream), TV_IDLE_POLL_HZ otherwise.
        self.fast = False
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
    def style_wanted(self) -> bool:
        """The Ambilight settings menu is only polled while this is True."""
        return self._style_wanted

    @style_wanted.setter
    def style_wanted(self, wanted: bool) -> None:
        if not wanted:
            self._menu = None  # never hand out stale settings when it is switched on again
            self._style_due = 0.0
        self._style_wanted = wanted

    @property
    def menu_settings(self) -> dict | None:
        """Ambilight settings menu nodes (node id -> data), polled at most every TV_STYLE_POLL_S."""
        return self._menu

    def read_times_ms(self) -> list[float]:
        """Response times of the most recent color reads."""
        return list(self._read_ms)

    def wake(self) -> None:
        """Poll now: forget the error backoff and end the current wait."""
        self._backoff = TV_BACKOFF_MIN_S
        self._wake.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="halo_sync_tv", daemon=True)
        self._thread.start()

    def stop(self, wait: bool = True) -> None:
        """Stop polling.

        With `wait`, this waits up to TV_REQUEST_TIMEOUT_S + 1 s for the thread. No further request
        starts after stop(); a request still running when the wait ends finishes in the background,
        and the thread closes the session once it is done (see _run).
        """
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            if wait and thread is not threading.current_thread():
                thread.join(timeout=TV_REQUEST_TIMEOUT_S + 1)
                if thread.is_alive():
                    _LOGGER.debug("TV reader still finishing a request; it closes the session when done")
        else:
            self._session.close()

    # ---- loop ------------------------------------------------------------
    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    delay = self._step()
                except _Stopped:
                    break
                except Exception:  # noqa: BLE001 - the reader must keep running
                    _LOGGER.exception("TV reader step failed")
                    delay = self._fail()
                if delay > 0:
                    self._wake.wait(delay)
                self._wake.clear()
        finally:
            self._session.close()

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise _Stopped

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
                self._check_stop()
            self._read_colors()
            self._check_stop()
            self._poll_style(t0)
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
            _LOGGER.debug("TV read failed: %s", exc)
            return self._fail()
        self._backoff = TV_BACKOFF_MIN_S
        # A completely black frame is a dark scene, not standby: polling goes on at the same rate.
        self._set_state("ok")
        period = self._fast_period if self.fast else self._idle_period
        return max(0.0, period - (self._clock() - t0))

    def _step_standby(self, t0: float) -> float:
        """Standby: check every TV_STANDBY_POLL_S whether the TV reports On again."""
        self._backoff = TV_BACKOFF_MIN_S
        if self._power_off():
            return TV_STANDBY_POLL_S
        self._check_stop()
        self._read_colors()
        self._power_due = t0 + TV_POWER_POLL_S
        self._set_state("ok")
        return 0.0

    def _fail(self) -> float:
        delay = self._backoff
        self._backoff = min(TV_BACKOFF_MAX_S, self._backoff * 2)
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
        """Read the Ambilight settings menu; a failure keeps the old value and leaves the state alone."""
        if not self._style_wanted or now < self._style_due:
            return
        self._style_due = now + TV_STYLE_POLL_S
        try:
            values = self._session.post(
                f"{self._base}/menuitems/settings/current",
                json={"nodes": [{"nodeid": n} for n in MENU_NODES]},
                timeout=TV_REQUEST_TIMEOUT_S,
            ).json()["values"]
            menu = {v["value"]["Nodeid"]: v["value"].get("data") or {} for v in values}
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
            _LOGGER.debug("TV style read failed: %s", exc)
            return
        if self._style_wanted:  # it may have been switched off while the request ran
            self._menu = menu
