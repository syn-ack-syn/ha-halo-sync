"""Ambilight -> Hue Entertainment streaming loop (runs in its own thread)."""

from __future__ import annotations

import logging
import math
import struct
import threading
import time
from collections.abc import Callable

import requests

from .const import CHROMA_FULL_LEVEL, DARK_CONTENT_LEVELS, DARK_LEVEL, SEND_HZ, TV_GIVEUP_S
from .dtls import DTLSAlert, DTLSClient, DTLSError
from .tv_reader import TvReader
from .zones import ZoneSampler

_LOGGER = logging.getLogger(__name__)

# A new session must not snapshot the lights while the previous one is still restoring them.
_RESTORE_LOCK = threading.Lock()
RESTORE_FADE_S = 0.8
FADE_IN_S = 0.8  # crossfade from the lights' own color to the streamed one when a session starts
RECONNECT_FIRST_S = 1.0  # pause before the first reconnect after the DTLS session broke
RECONNECT_MAX_S = 30.0
RECONNECT_TIMEOUT_S = 2.0  # DTLS timeout per flight while reconnecting, so a stop is not held up long
SESSION_CHECK_S = 10.0  # how often the bridge is asked whether our entertainment session is still active
SESSION_HTTP_TIMEOUT = (1, 1)  # connect, read
INACTIVE_WINDOW_S = 60.0  # the area stopping again this soon after a reconnect counts as a repeat
INACTIVE_REPEATS_MAX = 3  # repeats in a row before the stream gives up (another app keeps taking the area)
ALERT_FATAL = 2
# Fatal alerts that mean the bridge does not accept our identity or key: unknown_psk_identity,
# decrypt_error, bad_record_mac. Others (internal_error, ...) may pass and keep the backoff.
KEY_ALERTS = frozenset({115, 51, 20})
KEY_SUSPECT_MAX_S = 300.0  # backoff cap while plain key alerts suggest the bridge rejects our key
KEY_SUSPECT_ERROR = "bridge rejects the client key?"


def next_reconnect_pause(pause: float, cap: float | None = None) -> float:
    """Pause before the next reconnect attempt after one failed: 1, 2, 4, ... capped at 30 s (or `cap`)."""
    return min(pause * 2, RECONNECT_MAX_S if cap is None else cap)


def key_alert(exc: BaseException) -> int | None:
    """The description of a fatal KEY_ALERTS alert, else None."""
    if isinstance(exc, DTLSAlert) and exc.level == ALERT_FATAL and exc.description in KEY_ALERTS:
        return exc.description
    return None


def rejects_our_key(exc: BaseException) -> bool:
    """Whether a failed reconnect means the bridge refuses our key for good.

    Only an authenticated (epoch 1) fatal KEY_ALERTS alert counts. Plain epoch 0 alerts can be
    forged by anyone on the LAN, so they never end the stream (see KEY_SUSPECT_ERROR).
    """
    return key_alert(exc) is not None and exc.authenticated


class _SessionMonitor:
    """Asks the bridge every SESSION_CHECK_S whether the entertainment area is still active.

    Runs in its own daemon thread with its own keep-alive session, so a slow bridge never delays
    the stream; it only sets `inactive`. One monitor per DTLS session: start it after the link is
    up, stop it when the link is dropped or the stream ends. The stream starts a new monitor only
    once the previous one has ended, so at most one is alive.

    stop() does not wait for a request in flight: the thread may outlive the bounded join until
    that request returns or times out (SESSION_HTTP_TIMEOUT). Once stopped it never sets `inactive`.
    """

    def __init__(self, url: str, headers: dict, appid: str):
        self.url, self.headers, self.appid = url, headers, appid
        self.inactive = threading.Event()
        self._stop = threading.Event()
        self._warned = False
        self._http: requests.Session | None = None
        self._lock = threading.Lock()
        self.started = False
        self._thread = threading.Thread(target=self._run, name="halo_sync_session", daemon=True)

    def start(self) -> None:
        self.started = True
        self._thread.start()

    def alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            http = self._http
        if http is not None:
            http.close()  # drops pooled connections; may cut a request in flight short, else its timeout ends it
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=0.5)

    def _run(self) -> None:
        try:
            while not self._stop.wait(SESSION_CHECK_S):
                with self._lock:
                    if self._http is None:
                        self._http = requests.Session()
                        self._http.headers.update(self.headers)
                        self._http.verify = False
                    http = self._http
                if self.check(http) is False and not self._stop.is_set():
                    self.inactive.set()
                    return
        except Exception as exc:  # noqa: BLE001 - e.g. a session closed under a running request
            _LOGGER.debug("Entertainment area monitor ended: %s", exc)
        finally:
            with self._lock:
                http, self._http = self._http, None
            if http is not None:
                http.close()

    def check(self, http) -> bool | None:
        """False if the area is no longer active, True if it is, None if the answer is unusable.

        A different active_streamer is only logged (once): which id the bridge puts there is not
        confirmed on real hardware, and acting on it could fight another app for the area.
        """
        try:
            data = http.get(self.url, timeout=SESSION_HTTP_TIMEOUT).json()["data"][0]
            if data.get("status") != "active":
                return False
            rid = (data.get("active_streamer") or {}).get("rid")
        except (requests.RequestException, KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
            _LOGGER.debug("Could not read the entertainment area status: %s", exc)
            return None
        if rid is not None and rid != self.appid and not self._warned:
            self._warned = True
            _LOGGER.warning("Entertainment area is streamed by %s, our application id is %s", rid, self.appid)
        return True


def _split_luma(c: list[float]) -> tuple[float, list[float]]:
    lum = max(c)
    return lum, ([v / lum for v in c] if lum > 1e-4 else [1.0, 1.0, 1.0])


_WARM_WHITE = (1.0, 0.85, 0.7)


def _smoothstep(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def tone(rgb: list[float], s: dict) -> list[float]:
    """Map a sampled color to the light: brightness response, min/max range, dark-scene handling.

    Custom presets use their own gain + curve; TV-style presets use values emulated from the
    TV's selected Ambilight style. Either way the result spans min_brightness..brightness.
    """
    lo, hi = s["min_brightness"] / 100, s["brightness"] / 100
    p = active_params(s)
    if s["source"] == "tv_style":
        tv = s["tv"]
        if tv["category"] == "off":
            return [0.0, 0.0, 0.0]  # Ambilight is switched off on the TV
        hi *= tv["brightness_factor"]
        lo = min(lo, hi)
        if tv["category"] == "color":
            return [c * hi for c in _WARM_WHITE]  # the TV does not expose its fixed color
    lum = max(rgb)
    if lum < DARK_LEVEL:
        # "Ambilight on dark content" (same Off/Low/Medium/High as the TV): off, or a dim glow,
        # scaled by the max brightness. It ramps into the normal curve at DARK_LEVEL: a hard
        # switch makes the lights flicker when a scene hovers around the threshold.
        glow = DARK_CONTENT_LEVELS[s["dark_content"]] / 100 * hi
        edge = lo + (hi - lo) * min(1.0, DARK_LEVEL * p["gain"]) ** p["curve"]
        out_l = glow + (edge - glow) * _smoothstep(lum / DARK_LEVEL)
    else:
        out_l = lo + (hi - lo) * min(1.0, lum * p["gain"]) ** p["curve"]
    if lum <= 1e-4:
        return [c * out_l for c in _WARM_WHITE]
    # Normalising a few 8-bit counts to full colour would turn noise into random vivid hues.
    k = _smoothstep(lum / CHROMA_FULL_LEVEL)
    return [(k * v / lum + (1 - k) * w) * out_l for v, w in zip(rgb, _WARM_WHITE)]


def active_params(s: dict) -> dict:
    """Shaping parameters: from the preset, or emulated from the TV's selected Ambilight style."""
    return s["tv"] if s["source"] == "tv_style" else s


def _xy_to_rgb(x: float, y: float, bri: float) -> list[float]:
    """Approximate display RGB (0..1, max channel = brightness) of a Hue xy color, for the start crossfade."""
    y = max(y, 1e-4)
    X, Z = x / y, (1 - x - y) / y
    r, g, b = 1.656492 * X - 0.354851 - 0.255038 * Z, -0.707196 * X + 1.655397 + 0.036152 * Z, 0.051713 * X - 0.121364 + 1.011530 * Z
    r, g, b = (max(0.0, v) for v in (r, g, b))
    m = max(r, g, b) or 1.0
    return [r / m * bri, g / m * bri, b / m * bri]


class _Smoother:
    """Time-based smoothing with separate time constants for brightness and hue."""

    def __init__(self, n: int):
        self.lum = [0.0] * n
        self.hue = [[1.0, 1.0, 1.0] for _ in range(n)]

    def step(self, targets: list[list[float]], dt: float, tau_c: float, tau_l: float) -> list[list[float]]:
        if len(targets) != len(self.lum):
            self.__init__(len(targets))
        ac, al = 1 - math.exp(-dt / tau_c), 1 - math.exp(-dt / tau_l)
        for j, t in enumerate(targets):
            tl, th = _split_luma(t)
            self.lum[j] += al * (tl - self.lum[j])
            self.hue[j] = [h + ac * (x - h) for h, x in zip(self.hue[j], th)]
        return [[h * self.lum[j] for h in self.hue[j]] for j in range(len(self.lum))]


# The TV's own temporal filter (libhwambilight amb_algo_applyIntegration, decoded 2026-11): after gamma
# (c^2) and gain the colour goes through a per-frame IIR in YCbCr, then Java takes sqrt for the
# brightness. Rates are Q12 alphas per HAL frame; the frame rate (50 Hz) is an inference.
HAL_FRAME_S = 0.02
HAL_RATE_Y_UP = 256 / 4096  # luminance rising is slower than falling
HAL_RATE_Y_DOWN = 410 / 4096
HAL_RATE_C = 512 / 4096  # Cb and Cr, both directions
HAL_MAX = 0xFF00 / 65536


class _NativeFilter:
    """Experimental: the TV's gamma -> YCbCr IIR -> sqrt chain, one state per light."""

    def __init__(self, n: int):
        self.y, self.u, self.v = [0.0] * n, [0.0] * n, [0.0] * n
        self.primed = False

    def step(self, targets: list[list[float]], dt: float, gain: float) -> list[list[float]]:
        """Return linear-light-equivalent RGB so that tone() with the same gain yields sqrt(filtered)."""
        if len(targets) != len(self.y):
            self.__init__(len(targets))
        g2 = gain * gain
        frames = max(dt, 0.001) / HAL_FRAME_S  # time based: the time constants hold even if the loop slips
        a_c = 1 - (1 - HAL_RATE_C) ** frames
        out = []
        for j, t in enumerate(targets):
            r, g, b = (min(HAL_MAX, g2 * v * v) for v in t)
            y = 0.299 * r + 0.587 * g + 0.114 * b
            u = -0.168736 * r - 0.331264 * g + 0.5 * b
            v = 0.5 * r - 0.418688 * g - 0.081312 * b
            if not self.primed:
                self.y[j], self.u[j], self.v[j] = y, u, v
            else:
                a_y = 1 - (1 - (HAL_RATE_Y_UP if y > self.y[j] else HAL_RATE_Y_DOWN)) ** frames
                self.y[j] += a_y * (y - self.y[j])
                self.u[j] += a_c * (u - self.u[j])
                self.v[j] += a_c * (v - self.v[j])
            # Chroma settles faster than rising luminance; scale it to what Y can hold, because
            # clamping single channels would flash a fully saturated colour.
            lum_y = self.y[j]
            chroma = (1.402 * self.v[j], -0.344136 * self.u[j] - 0.714136 * self.v[j], 1.772 * self.u[j])
            low = min(chroma)
            scale = lum_y / -low if low < 0 and lum_y + low < 0 else 1.0
            fr, fg, fb = (min(1.0, max(0.0, lum_y + scale * c)) for c in chroma)
            m = max(fr, fg, fb)
            if m <= 1e-6:
                out.append([0.0, 0.0, 0.0])
                continue
            eq = math.sqrt(m) / gain
            out.append([fr / m * eq, fg / m * eq, fb / m * eq])
        self.primed = True
        return out


# Lag of the TV's own Ambilight+Hue (measured 2026-11): the TV sends one lamp every 40-80 ms over
# the bridge's REST API (each lamp gets an update roughly every 0.8 s) and every update is a Hue
# transition of 0.8 s (1.3 s when the light gets dimmer).
NATIVE_HOLD_S = 0.8
NATIVE_RISE_S = 0.8
NATIVE_FALL_S = 1.3


class _Ramp:
    """Emulates that lag: sample-and-hold per light + a linear transition after every update.

    `delay` scales it from 0 (pass through) to 1 (as slow as the TV's own Ambilight+Hue).
    """

    def __init__(self, n: int):
        self.out = [[0.0, 0.0, 0.0] for _ in range(n)]
        self.start = [list(o) for o in self.out]
        self.target = [list(o) for o in self.out]
        self.t0 = [0.0] * n
        self.dur = [0.0] * n
        self.due = [0.0] * n
        self._primed = False

    def step(self, now: float, targets: list[list[float]], delay: float) -> list[list[float]]:
        n = len(targets)
        if n != len(self.out):
            self.__init__(n)
        if delay <= 0.001 or not self._primed:
            self._primed = delay > 0.001 and bool(targets)
            self.out = [list(t) for t in targets]
            self.start = [list(t) for t in targets]
            self.target = [list(t) for t in targets]
            return self.out
        hold = NATIVE_HOLD_S * delay
        for j, tgt in enumerate(targets):
            if now >= self.due[j]:
                # Stagger the lights like the TV's one-lamp-at-a-time queue.
                self.due[j] = now + hold if self.due[j] else now + hold * (j + 1) / n
                cur = self.out[j]
                self.start[j], self.target[j], self.t0[j] = list(cur), list(tgt), now
                falling = max(tgt) < max(cur)
                self.dur[j] = (NATIVE_FALL_S if falling else NATIVE_RISE_S) * delay
            k = 1.0 if self.dur[j] <= 0 else min(1.0, (now - self.t0[j]) / self.dur[j])
            self.out[j] = [s + (t - s) * k for s, t in zip(self.start[j], self.target[j])]
        return self.out


class Streamer:
    """Owns one streaming session at a time; start/stop are thread-safe."""

    def __init__(self, cfg: dict, settings: dict, on_change: Callable[[], None], *, reader: TvReader):
        self.cfg = cfg
        self.reader = reader  # owned by the manager; the stream only reads its latest frame
        self.settings = settings  # live preset values, read every frame
        self.extra_positions: list[tuple[float, float]] = []  # non-Hue lights, set by the manager
        self.extra_out: list[list[float]] = []  # smoothed RGB (0..1, brightness applied) for those lights
        self._orig: dict[int, list[float]] = {}  # channel id -> RGB the light had before streaming
        self._pos: dict[int, tuple[float, float]] = {}  # channel id -> (x, z), refreshed from the bridge
        self._on_change = on_change
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False
        self.error: str | None = None
        self.stats: dict = {}
        self.link = "connected"  # DTLS session to the bridge: "connected" or "reconnecting"

    @property
    def _cfg_url(self) -> str:
        return f"https://{self.cfg['hue_host']}/clip/v2/resource/entertainment_configuration/{self.cfg['entertainment_id']}"

    @property
    def _hue_headers(self) -> dict:
        return {"hue-application-key": self.cfg["hue_user"]}

    def channels(self) -> list[dict]:
        data = requests.get(self._cfg_url, headers=self._hue_headers, verify=False, timeout=5).json()["data"][0]
        return [{"id": c["channel_id"], "x": c["position"]["x"], "z": c["position"]["z"]} for c in data["channels"]]

    def refresh_positions(self) -> list[dict]:
        """Re-read channel positions from the bridge; a running stream picks them up on its next TV frame."""
        chans = self.channels()
        self._pos = {c["id"]: (c["x"], c["z"]) for c in chans}
        return chans

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self.running, self.error, self.stats, self.link = True, None, {}, "connected"
            self._thread = threading.Thread(target=self._run, name="halo_sync", daemon=True)
            self._thread.start()
        self._on_change()

    def stop(self, wait: bool = False) -> None:
        self._stop.set()
        thread = self._thread
        if wait and thread and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _light_url(self, rid: str) -> str:
        return f"https://{self.cfg['hue_host']}/clip/v2/resource/light/{rid}"

    def _snapshot_lights(self) -> dict[str, dict]:
        """Remember the on/brightness/color of the lights in the entertainment area (Hue does not restore them)."""
        snap: dict[str, dict] = {}
        try:
            http = requests.Session()  # one connection: a TLS handshake per request is slow on the bridge
            http.headers.update(self._hue_headers)
            http.verify = False
            area = http.get(self._cfg_url, timeout=5).json()["data"][0]
            wanted = {ref["rid"] for ref in area.get("light_services", [])}
            lights = http.get(f"https://{self.cfg['hue_host']}/clip/v2/resource/light", timeout=5).json()["data"]
            ent_owner = {e["id"]: e["owner"]["rid"] for e in http.get(
                f"https://{self.cfg['hue_host']}/clip/v2/resource/entertainment", timeout=5).json()["data"]}
            light_by_owner = {d["owner"]["rid"]: d for d in lights if d["id"] in wanted}
            self._orig = {}
            for ch in area.get("channels", []):
                data = light_by_owner.get(ent_owner.get(ch["members"][0]["service"]["rid"]))
                if not data:
                    continue
                if not data["on"]["on"]:
                    self._orig[ch["channel_id"]] = [0.0, 0.0, 0.0]
                elif "color" in data and "dimming" in data:
                    xy = data["color"]["xy"]
                    self._orig[ch["channel_id"]] = _xy_to_rgb(xy["x"], xy["y"], data["dimming"]["brightness"] / 100)
            for data in lights:
                if data["id"] not in wanted:
                    continue
                state: dict = {"on": {"on": data["on"]["on"]}}
                if data["on"]["on"]:
                    if "dimming" in data:
                        state["dimming"] = {"brightness": max(1.0, data["dimming"]["brightness"])}
                    ct = data.get("color_temperature")
                    if ct and ct.get("mirek_valid") and ct.get("mirek"):
                        state["color_temperature"] = {"mirek": ct["mirek"]}
                    elif "color" in data:
                        state["color"] = {"xy": data["color"]["xy"]}
                snap[data["id"]] = state
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            _LOGGER.warning("Could not snapshot the Hue lights, they will not be restored: %s", exc)
            self._orig = {}
            return {}
        return snap

    def _restore_lights(self, snap: dict[str, dict]) -> None:
        for rid, state in snap.items():
            try:
                requests.put(
                    self._light_url(rid),
                    json={**state, "dynamics": {"duration": int(RESTORE_FADE_S * 1000)}},
                    headers=self._hue_headers, verify=False, timeout=5,
                )
            except requests.RequestException as exc:
                _LOGGER.warning("Could not restore Hue light %s: %s", rid, exc)

    def _run(self) -> None:
        snap: dict[str, dict] = {}
        # The reader polls at full rate only while a stream needs it; wake it from a slow wait or
        # backoff. The finally below hands the rate back whatever fails, the snapshot included.
        self.reader.fast = True
        self.reader.wake()
        try:
            with _RESTORE_LOCK:
                snap = self._snapshot_lights()
            self._stream()
        except Exception as exc:  # noqa: BLE001 - surface any failure on the switch
            _LOGGER.warning("Halo Sync stopped: %s", exc)
            self.error = str(exc)
        finally:
            self.reader.fast = False
            self._stop.set()
            try:
                requests.put(self._cfg_url, json={"action": "stop"}, headers=self._hue_headers, verify=False, timeout=5)
            except requests.RequestException:
                pass
            self.running = False
            self.extra_out = []
            self._on_change()
            if snap:
                with _RESTORE_LOCK:
                    self._restore_lights(snap)
                    time.sleep(0.4)
                    if not self.running:  # the bridge may still be settling from the stream; repeat once
                        self._restore_lights(snap)

    def _open_link(self, timeout: float = 5.0) -> DTLSClient | None:
        """Start the entertainment area on the bridge and open the DTLS session to it.

        Returns None without a handshake if the stream was stopped meanwhile.
        """
        cfg = self.cfg
        r = requests.put(self._cfg_url, json={"action": "start"}, headers=self._hue_headers, verify=False, timeout=5)
        if r.status_code != 200:
            raise RuntimeError(f"bridge refused to start the entertainment area: {r.status_code} {r.text}")
        if self._stop.is_set():
            return None
        client = DTLSClient(cfg["hue_host"], 2100, cfg["hue_appid"], bytes.fromhex(cfg["hue_clientkey"]), timeout)
        try:
            client.connect()
        except BaseException:
            client.close()
            raise
        return client

    def _set_link(self, link: str, reason: str | None = None) -> None:
        """Report the bridge connection state through on_change (and the stats the entities show)."""
        self.link = link
        stats = {k: v for k, v in self.stats.items() if k not in ("hue_link", "hue_link_error")}
        if link != "connected":
            stats["hue_link"] = link
            if reason:
                stats["hue_link_error"] = reason
        self.stats = stats
        self._on_change()

    def _stream(self) -> None:
        cfg = self.cfg
        chans = self.refresh_positions()
        latest: dict = {"hue": None, "extra": [], "seen": None}
        tv = self.reader
        sampler, sampler_extra = ZoneSampler(), ZoneSampler()
        mono_start = time.monotonic()
        standby_since: float | None = None
        reads_at_stats = tv.read_count

        def sample_new_frame(mono: float) -> None:
            """Sample the reader's latest frame once, if it is new and not older than TV_GIVEUP_S."""
            layer, at = tv.latest_measured, tv.latest_at
            if layer is None or at is None or at == latest["seen"] or mono - at > TV_GIVEUP_S:
                return
            latest["seen"] = at
            s_ = self.settings
            sat = active_params(s_)["saturation"]
            mode, spread, count = s_.get("zone_mode", "smooth"), s_["spread"], int(s_.get("zone_count", 3))
            nz = s_.get("native_zones") or {}
            hue_pos = [self._pos.get(c["id"], (c["x"], c["z"])) for c in chans]
            hue_zones = [nz.get(str(c["id"])) for c in chans]
            extra_pos = list(self.extra_positions)
            extra_zones = [None] * len(extra_pos)
            latest["hue"] = sampler.sample(layer, hue_pos, mode, spread, count, hue_zones, sat)
            latest["extra"] = sampler_extra.sample(layer, extra_pos, mode, spread, count, extra_zones, sat)

        # A failure here ends the session with an error (wrong key, bridge busy); only a session
        # that was up reconnects by itself.
        client: DTLSClient | None = self._open_link()
        if client is None:
            return  # stopped while the bridge started the area; _run cleans up
        pause, retry_at = RECONNECT_FIRST_S, 0.0
        last_key_alert: int | None = None  # plain fatal key alert of the previous reconnect attempt
        key_suspect = False  # the same plain key alert came twice in a row
        inactive_link_at: float | None = None  # link restored after the area had stopped
        inactive_repeats = 0
        dropped_inactive = False
        monitor: _SessionMonitor | None = None
        last_started: _SessionMonitor | None = None  # the newest monitor whose thread was started

        def new_monitor() -> _SessionMonitor:
            """A monitor for the new link; started now, or by the loop once the previous one ended."""
            nonlocal last_started
            m = _SessionMonitor(self._cfg_url, self._hue_headers, cfg["hue_appid"])
            if last_started is None or not last_started.alive():
                m.start()
                last_started = m
            return m

        def drop_link(reason: str) -> None:
            nonlocal client, retry_at
            _LOGGER.warning("Halo Sync lost the bridge connection, reconnecting in %.0f s: %s", pause, reason)
            monitor.stop()
            client.close()
            client = None
            retry_at = time.monotonic() + pause
            self._set_link("reconnecting", reason)

        _LOGGER.info("Halo Sync started (%d channels)", len(chans))

        ent_id = cfg["entertainment_id"].encode()
        hue_s, extra_s = _Smoother(len(chans)), _Smoother(0)
        hue_r, extra_r = _Ramp(len(chans)), _Ramp(0)
        hue_n, extra_n = _NativeFilter(len(chans)), _NativeFilter(0)
        seq, last, last_stats = 0, time.time(), time.time()
        t_start, primed = time.time(), False
        try:
            monitor = new_monitor()
            while not self._stop.is_set():
                now = time.time()
                dt, last = now - last, now
                mono = time.monotonic()
                if client is not None and not monitor.started and not last_started.alive():
                    monitor.start()  # the previous monitor (stuck in a request) has ended now
                    last_started = monitor
                if client is None and mono >= retry_at:
                    # Restart the entertainment area as well: the bridge may still hold it for the
                    # broken session. The reader keeps its full rate meanwhile (fast stays set).
                    # Every blocking step checks for a stop first; _run then cleans up.
                    try:
                        requests.put(self._cfg_url, json={"action": "stop"}, headers=self._hue_headers, verify=False, timeout=5)
                    except requests.RequestException:
                        pass
                    if self._stop.is_set():
                        break
                    try:
                        client = self._open_link(RECONNECT_TIMEOUT_S)
                        if client is None:
                            break
                    except (DTLSError, OSError, RuntimeError) as exc:
                        if rejects_our_key(exc):
                            # Authenticated: the bridge refuses our identity or key, retrying cannot help.
                            raise RuntimeError(f"bridge rejected the reconnect: {exc}") from exc
                        code = key_alert(exc)
                        if code is not None and code == last_key_alert and not key_suspect:
                            # Twice the same plain key alert: likely a wrong key, but it could be
                            # forged, so show it and keep trying slowly instead of giving up.
                            key_suspect = True
                            self.error = KEY_SUSPECT_ERROR
                        last_key_alert = code
                        pause = next_reconnect_pause(pause, KEY_SUSPECT_MAX_S if key_suspect else None)
                        retry_at = time.monotonic() + pause
                        _LOGGER.warning("Halo Sync could not reconnect to the bridge (next try in %.0f s): %s", pause, exc)
                        self._set_link("reconnecting", str(exc))
                    else:
                        pause, last_key_alert = RECONNECT_FIRST_S, None
                        if key_suspect:
                            key_suspect = False
                            if self.error == KEY_SUSPECT_ERROR:
                                self.error = None
                        inactive_link_at = time.monotonic() if dropped_inactive else None
                        monitor = new_monitor()
                        _LOGGER.info("Halo Sync reconnected to the bridge")
                        self._set_link("connected")
                elif client is not None and monitor.inactive.is_set():
                    # A session the bridge ended quietly (another app, bridge restart) sends no
                    # error; the monitor thread noticed the area is no longer active.
                    if inactive_link_at is not None and mono - inactive_link_at < INACTIVE_WINDOW_S:
                        inactive_repeats += 1
                    else:
                        inactive_repeats = 0
                    if inactive_repeats >= INACTIVE_REPEATS_MAX:
                        raise RuntimeError("entertainment area keeps stopping, another app?")
                    dropped_inactive = True
                    drop_link("the bridge no longer streams this session")
                if tv.state == "standby":
                    standby_since = mono if standby_since is None else standby_since
                    if mono - standby_since > TV_GIVEUP_S:
                        _LOGGER.info("Halo Sync stopped: the TV is in standby")
                        self.error = "TV in standby"
                        return  # _run stops the entertainment area and restores the lights
                else:
                    standby_since = None
                    # In standby (/powerstate is not On) the reader reads no colors, so stale frames
                    # are only checked outside it. A TV that is switched off completely does not
                    # answer at all: the reader is unreachable and this ends the stream with an error.
                    if mono - max(tv.latest_at or 0.0, mono_start) > TV_GIVEUP_S:
                        raise RuntimeError("TV stopped answering")
                sample_new_frame(mono)
                p = active_params(self.settings)
                tau_c = max(p["smoothing_color"], 1) / 1000
                tau_l = max(p["smoothing_brightness"], 1) / 1000
                s = self.settings
                if latest["hue"]:
                    delay = min(1.0, max(0.0, float(s.get("native_delay", 0)) / 100))
                    native_f = s.get("smoothing_mode") == "native" and s["source"] == "video"
                    if native_f:
                        smoothed = hue_n.step(latest["hue"], dt, s["gain"])
                    else:
                        if not primed:  # start at the first frame's color, not from black; the crossfade below softens it
                            hue_s.step(latest["hue"], 10.0, tau_c, tau_l)
                            extra_s.step(latest["extra"], 10.0, tau_c, tau_l)
                            primed = True
                        smoothed = hue_s.step(latest["hue"], dt, tau_c, tau_l)
                    cur = [tone(rgb, s) for rgb in smoothed]
                    cur = hue_r.step(now, cur, delay)
                    k = min(1.0, (now - t_start) / FADE_IN_S)
                    if k < 1.0:
                        k = k * k * (3 - 2 * k)
                        cur = [
                            [o + k * (v - o) for o, v in zip(self._orig[c["id"]], cur[j])] if c["id"] in self._orig else cur[j]
                            for j, c in enumerate(chans)
                        ]
                    body = b"".join(
                        struct.pack(">BHHH", c["id"], *[min(65535, int(v * 65535)) for v in cur[j]])
                        for j, c in enumerate(chans)
                    )
                    if client is not None and not self._stop.is_set():
                        try:
                            client.send(b"HueStream" + bytes([2, 0, seq & 255, 0, 0, 0, 0]) + ent_id + body)
                        except (DTLSError, OSError) as exc:  # includes alerts the bridge sent (close_notify)
                            dropped_inactive = False
                            drop_link(str(exc))
                        else:
                            seq += 1
                if latest["extra"]:
                    delay = min(1.0, max(0.0, float(s.get("native_delay", 0)) / 100))
                    native_f = s.get("smoothing_mode") == "native" and s["source"] == "video"
                    if native_f:
                        smoothed_x = extra_n.step(latest["extra"], dt, s["gain"])
                    else:
                        smoothed_x = extra_s.step(latest["extra"], dt, tau_c, tau_l)
                    cur_x = [tone(rgb, s) for rgb in smoothed_x]
                    self.extra_out = extra_r.step(now, cur_x, delay)
                if now - last_stats > 10 and tv.read_count > reads_at_stats:
                    v = sorted(tv.read_times_ms())
                    n = tv.read_count - reads_at_stats
                    link = {k: v for k, v in self.stats.items() if k in ("hue_link", "hue_link_error")}
                    self.stats = {"tv_reads_per_s": round(n / (now - last_stats), 1), "tv_read_median_ms": round(v[len(v) // 2]) if v else None, **link}
                    reads_at_stats = tv.read_count
                    last_stats = now
                    self._on_change()
                time.sleep(1 / SEND_HZ)
        finally:
            if monitor is not None:
                monitor.stop()
            if client is not None:
                client.close()
