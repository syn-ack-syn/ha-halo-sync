"""Presets, start/stop and the non-Hue light driver for one config entry."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import time

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import (
    CHANNEL_REFRESH_S,
    CONF_TV_ENDPOINT,
    DARK_CONTENT_LEVELS,
    DEFAULT_ACTIVE,
    DEFAULT_PRESETS,
    DEFAULTS_VERSION,
    DOMAIN,
    EXTRA_LIGHT_DEFAULTS,
    EXTRA_LIGHT_MAX_HZ,
    SMOOTHING_MODES,
    SOURCES,
    TUNABLE_FALLBACK,
    TUNABLES,
    TV_MENU_BRIGHTNESS,
    TV_MENU_CATEGORIES,
    TV_MENU_SATURATION,
    TV_MENU_STYLE,
    TV_MENU_VIDEO_STYLE,
    TV_ENDPOINT,
    TV_STYLE_DEFAULT,
    TV_STYLE_POLL_S,
    TV_VIDEO_STYLES,
    ZONE_MODES,
)

# Default presets introduced after the first release, keyed by the defaults version that added them.
_ADDED_IN = {"tv_style": 2, "native": 3}
from . import nettest
from .resume import AutoResume
from .streamer import Streamer, ends_with_lights_off
from .tv_reader import TvReader

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1


def _clean_preset(raw: dict, name_fallback: str) -> dict:
    out = {"name": str(raw.get("name") or name_fallback)[:40]}
    # Missing keys (e.g. tunables added later) come from the matching default preset.
    base = DEFAULT_PRESETS.get(name_fallback, DEFAULT_PRESETS["game"])
    for key, (lo, hi, _) in TUNABLES.items():
        try:
            out[key] = min(hi, max(lo, float(raw.get(key, base.get(key, TUNABLE_FALLBACK.get(key))))))
        except (TypeError, ValueError):
            out[key] = base.get(key, TUNABLE_FALLBACK.get(key))
    out["source"] = raw.get("source") if raw.get("source") in SOURCES else base["source"]
    out["zone_mode"] = raw.get("zone_mode") if raw.get("zone_mode") in ZONE_MODES else base.get("zone_mode", "smooth")
    out["smoothing_mode"] = (
        raw.get("smoothing_mode") if raw.get("smoothing_mode") in SMOOTHING_MODES
        else base.get("smoothing_mode", "standard")
    )
    zones = {}
    for ch, zone in (raw.get("native_zones") or {}).items():
        try:
            if 0 <= int(zone) <= 9:
                zones[str(int(ch))] = int(zone)
        except (TypeError, ValueError):
            continue
    out["native_zones"] = zones
    dark = raw.get("dark_content")
    legacy = raw.get("dark_mode")
    if legacy is not None and out["source"] == "video":
        # Custom presets saved before "Dark scenes" was merged into "Ambilight on dark content":
        # their only dark-scene choice was the old setting (dark_mode is dropped on the next save).
        floor = out["min_brightness"]
        if legacy == "off":
            dark = "off"
        else:
            dark = "low" if floor <= 3 else "medium" if floor <= 6 else "high"
    elif dark not in DARK_CONTENT_LEVELS:
        dark = base["dark_content"]
    out["dark_content"] = dark
    lights = []
    for light in raw.get("extra_lights") or []:
        entity_id = str(light.get("entity_id", ""))
        if not entity_id.startswith("light."):
            continue
        item = {"entity_id": entity_id}
        for key, default in EXTRA_LIGHT_DEFAULTS.items():
            try:
                item[key] = float(light.get(key, default))
            except (TypeError, ValueError):
                item[key] = default
        item["x"] = min(1.5, max(-1.5, item["x"]))
        item["z"] = min(1.5, max(-1.5, item["z"]))
        item["update_hz"] = min(EXTRA_LIGHT_MAX_HZ, max(0.2, item["update_hz"]))
        item["transition"] = min(5.0, max(0.0, item["transition"]))
        effect = str(light.get("effect") or "").strip()
        if effect:
            item["effect"] = effect
        lights.append(item)
    out["extra_lights"] = lights
    return out


class Manager:
    def __init__(self, hass: HomeAssistant, entry, signal: str):
        self.hass = hass
        self.entry = entry
        self.signal = signal
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.presets: dict[str, dict] = {}
        self.active = DEFAULT_ACTIVE
        # Live values of the active preset, read by the streamer thread; "tv" = emulated TV style.
        self.settings: dict = {"tv": dict(TV_STYLE_DEFAULT)}
        # The only connection to the TV: stream, color sensor and TV style all read from it.
        d = entry.data
        self.reader = TvReader(
            d["tv_host"], d["tv_user"], d["tv_pass"],
            endpoint=entry.options.get(CONF_TV_ENDPOINT, TV_ENDPOINT),
            on_change=self._notify_reader_threadsafe,
        )
        self.streamer = Streamer(dict(entry.data), self.settings, self._notify_threadsafe, reader=self.reader)
        self.adb = None
        self.hue_channels: list[dict] = []
        self._extra_task: asyncio.Task | None = None
        self._snapshot = False
        self._extra_ids: list[str] = []  # entities in the current scene snapshot
        # A restore and the next snapshot must not interleave: starting right after a preset change
        # would let the pending restore use up the new snapshot, leaving nothing to restore on stop.
        self._extra_lock = asyncio.Lock()
        self._extra_calls: set[asyncio.Task] = set()
        self._was_running = False
        self.tv_color: tuple[int, int, int] | None = None
        self._nettest_lock = asyncio.Lock()
        self.resume = AutoResume()
        # Turn the lights off instead of restoring them when the stream ends because the TV went off.
        self.lights_off_with_tv = False
        # Set by _async_persist_resume (unload or HA stop): the stream ending now is no manual stop.
        self._unloading = False

    # ---- state ---------------------------------------------------------
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        presets = data.get("presets") or copy.deepcopy(DEFAULT_PRESETS)
        seen = data.get("defaults_version", 1 if data else DEFAULTS_VERSION)
        for pid, version in _ADDED_IN.items():
            if seen < version and pid not in presets:
                presets[pid] = copy.deepcopy(DEFAULT_PRESETS[pid])
        self.presets = {pid: _clean_preset(p, pid) for pid, p in presets.items()}
        self.resume = AutoResume(enabled=data.get("auto_resume", True), pending=data.get("resume_pending", False))
        self.lights_off_with_tv = bool(data.get("lights_off_with_tv", False))
        self.streamer.lights_off_with_tv = self.lights_off_with_tv
        if seen < DEFAULTS_VERSION or any("dark_mode" in p for p in presets.values()):
            self._save()  # persists newly added defaults and the dark_mode -> dark_content migration
        for candidate in (data.get("active"), DEFAULT_ACTIVE, next(iter(self.presets))):
            if candidate in self.presets:
                self.active = candidate
                break
        self._apply_active()

    def _data(self) -> dict:
        return {
            "active": self.active,
            "presets": self.presets,
            "defaults_version": DEFAULTS_VERSION,
            "auto_resume": self.resume.enabled,
            "resume_pending": self.resume.pending,
            "lights_off_with_tv": self.lights_off_with_tv,
        }

    def _save(self) -> None:
        self._store.async_delay_save(self._data, 1)

    def _apply_active(self) -> None:
        p = self.presets[self.active]
        self.settings.update(
            {k: p[k] for k in TUNABLES},
            source=p["source"],
            dark_content=p["dark_content"],
            zone_mode=p["zone_mode"],
            native_zones=p["native_zones"],
            smoothing_mode=p["smoothing_mode"],
        )
        self.streamer.extra_positions = [(light["x"], light["z"]) for light in p["extra_lights"]]

    @callback
    def notify(self) -> None:
        async_dispatcher_send(self.hass, self.signal)

    def _notify_threadsafe(self) -> None:
        self.hass.loop.call_soon_threadsafe(self._on_streamer_change)

    def _notify_reader_threadsafe(self) -> None:
        self.hass.loop.call_soon_threadsafe(self._on_reader_change)

    @callback
    def _on_reader_change(self) -> None:
        self.notify()
        self._maybe_resume()

    @callback
    def _maybe_resume(self) -> None:
        if self._unloading:
            return
        if self.resume.should_start(self.reader.state, self.streamer.running, time.monotonic()):
            _LOGGER.info("Halo Sync: TV is back, resuming the stream")
            self.hass.async_create_task(self._async_resume())

    async def _async_resume(self) -> None:
        # A manual stop or an unload between the decision and this task wins.
        if not self.resume.pending or self._unloading:
            return
        await self.hass.async_add_executor_job(self.streamer.start)
        # A start that failed before _on_streamer_change saw it running never reached stream_ended there.
        if (
            not self.streamer.running
            and not self._was_running
            and not self._unloading
            and self.resume.stream_ended(self.streamer.stop_reason)
        ):
            self._save()
            self.notify()

    @callback
    def _on_streamer_change(self) -> None:
        running = self.streamer.running
        if running != self._was_running:
            self._was_running = running
            if running:
                self._start_extra()
                if self.resume.stream_started():
                    self._save()
            else:
                self.hass.async_create_task(self._stop_extra())
                if not self._unloading and self.resume.stream_ended(self.streamer.stop_reason):
                    self._save()
                    # The reader may have reported "ok" before this callback; that change would be lost.
                    self._maybe_resume()
        self.notify()

    def status(self) -> dict:
        s = self.streamer
        return {
            "running": s.running,
            "error": s.error,
            "stats": s.stats,
            "tv_source": self.adb.source if self.adb else None,
            "tv_style": self.settings.get("tv") if self.settings.get("source") == "tv_style" else None,
            "active": self.active,
            "presets": self.presets,
            "hue_channels": self.hue_channels,
            "auto_resume": self.resume.enabled,
            "resume_pending": self.resume.pending,
            "lights_off_with_tv": self.lights_off_with_tv,
        }

    # ---- presets -------------------------------------------------------
    @callback
    def activate(self, preset_id: str) -> None:
        if preset_id not in self.presets:
            raise KeyError(preset_id)
        self.active = preset_id
        self._apply_active()
        self._restart_extra()
        self._save()
        self.notify()

    def find_by_name(self, name: str) -> str | None:
        return next((pid for pid, p in self.presets.items() if p["name"].lower() == name.lower()), None)

    @callback
    def save_preset(self, preset_id: str | None, preset: dict) -> str:
        if not preset_id:
            base = re.sub(r"[^a-z0-9]+", "_", str(preset.get("name", "preset")).lower()).strip("_") or "preset"
            preset_id, n = base, 2
            while preset_id in self.presets:
                preset_id, n = f"{base}_{n}", n + 1
        self.presets[preset_id] = _clean_preset(preset, preset_id)
        if preset_id == self.active:
            self._apply_active()
            self._restart_extra()
        self._save()
        self.notify()
        return preset_id

    @callback
    def delete_preset(self, preset_id: str) -> None:
        if len(self.presets) <= 1 or preset_id not in self.presets:
            raise ValueError("cannot delete the last preset")
        del self.presets[preset_id]
        if self.active == preset_id:
            self.active = next(iter(self.presets))
            self._apply_active()
            self._restart_extra()
        self._save()
        self.notify()

    @callback
    def reset_defaults(self) -> None:
        self.presets = {pid: _clean_preset(p, pid) for pid, p in copy.deepcopy(DEFAULT_PRESETS).items()}
        self.active = DEFAULT_ACTIVE
        self._apply_active()
        self._restart_extra()
        self._save()
        self.notify()

    # ---- run -----------------------------------------------------------
    async def async_start(self) -> None:
        await self.hass.async_add_executor_job(self.streamer.start)

    async def async_connection_test(self, count: int = 20) -> dict:
        """Ping the TV, read its Ambilight endpoint and probe the Hue bridge (see nettest.py)."""
        async with self._nettest_lock:
            d = self.entry.data
            return await nettest.run(self.hass, d["tv_host"], d["tv_user"], d["tv_pass"], d["hue_host"], count)

    async def async_stop(self) -> None:
        await self.hass.async_add_executor_job(self.streamer.stop, True)
        # After the stop: a stream ending meanwhile must not leave the resume pending.
        if self.resume.user_stopped():
            self._save()
            self.notify()

    async def async_shutdown(self) -> None:
        """Stop for a reload or disable of the entry: a stream that was running or waiting resumes afterwards.

        HA stopping does not unload the entry; that path is _async_on_ha_stop.
        """
        await self._async_persist_resume()
        await self.hass.async_add_executor_job(self.streamer.stop, True)

    async def _async_on_ha_stop(self, event: Event) -> None:
        await self._async_persist_resume()

    async def _async_persist_resume(self) -> None:
        """Mark a running or waiting stream as pending and save it now; not a manual stop."""
        self._unloading = True
        if self.resume.enabled and (self.resume.pending or self.streamer.running):
            self.resume.pending = True
        # A delayed save is not reliably written while unloading or stopping.
        await self._store.async_save(self._data())

    async def async_set_auto_resume(self, enabled: bool) -> None:
        if self.resume.set_enabled(enabled):
            self._save()
        self.notify()

    async def async_set_lights_off_with_tv(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled != self.lights_off_with_tv:
            self.lights_off_with_tv = enabled
            self.streamer.lights_off_with_tv = enabled
            self._save()
        self.notify()

    async def async_refresh_channels(self) -> None:
        try:
            channels = await self.hass.async_add_executor_job(self.streamer.refresh_positions)
        except Exception as exc:  # noqa: BLE001 - bridge may be briefly unreachable
            _LOGGER.debug("could not read entertainment channels: %s", exc)
            return
        if channels != self.hue_channels:
            self.hue_channels = channels
            self.notify()

    async def _channel_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(CHANNEL_REFRESH_S)
            await self.async_refresh_channels()

    # ---- TV Ambilight style (read from the TV settings menu) -----------
    async def _tv_style_loop(self) -> None:
        def style_from_menu(data: dict) -> dict:
            category = TV_MENU_CATEGORIES.get(data.get(TV_MENU_STYLE, {}).get("activenode_id"), "video")
            style = data.get(TV_MENU_VIDEO_STYLE, {}).get("selected_item", 1)
            # Follow audio cannot be mirrored (no audio data), so it falls back to Standard on video.
            params = TV_VIDEO_STYLES.get(style if category == "video" else 1, TV_VIDEO_STYLES[1])
            bri = data.get(TV_MENU_BRIGHTNESS, {}).get("value", 10)
            sat = data.get(TV_MENU_SATURATION, {}).get("value", 0)
            return {
                **params,
                "category": category,
                "style": style,
                "brightness_factor": min(10, max(0, bri)) / 10 if bri <= 10 else 1.0,  # 255 = not set
                "saturation": params["saturation"] * (1 + 0.15 * max(-2, min(2, sat))),
            }

        while True:
            # The reader polls the TV settings menu only while a TV-style preset is active.
            self.reader.style_wanted = self.settings.get("source") == "tv_style"
            data = self.reader.menu_settings if self.reader.style_wanted else None
            if data is not None:
                tv = style_from_menu(data)
                if tv != self.settings.get("tv"):
                    self.settings["tv"] = tv
                    self.notify()
            await asyncio.sleep(TV_STYLE_POLL_S)

    # ---- average screen color (for dashboards) -------------------------
    def start_color_poll(self) -> None:
        self.reader.start()
        self.entry.async_on_unload(self._async_stop_reader)
        self.entry.async_on_unload(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_on_ha_stop))
        self.entry.async_create_background_task(self.hass, self._color_loop(), f"{DOMAIN}_tv_color")
        self.entry.async_create_background_task(self.hass, self._tv_style_loop(), f"{DOMAIN}_tv_style")
        self.entry.async_create_background_task(self.hass, self._channel_refresh_loop(), f"{DOMAIN}_channels")

    async def _async_stop_reader(self) -> None:
        await self.hass.async_add_executor_job(self.reader.stop)

    def _average_color(self) -> tuple[int, int, int] | None:
        layer = self.reader.latest_measured
        if self.reader.state != "ok" or not layer:
            return None
        try:
            px = [c for side in layer.values() for c in side.values()]
            return tuple(round(sum(c[k] for c in px) / len(px)) for k in ("r", "g", "b")) if px else None
        except (AttributeError, KeyError, TypeError):
            return None

    async def _color_loop(self) -> None:
        # The sensor itself limits how often its state changes (sensor.py).
        while True:
            color = self._average_color()
            if color != self.tv_color:
                self.tv_color = color
                self.notify()
            await asyncio.sleep(1)

    # ---- non-Hue lights --------------------------------------------------
    def _extra_lights(self) -> list[dict]:
        return self.presets[self.active]["extra_lights"]

    @callback
    def _start_extra(self) -> None:
        lights = self._extra_lights()
        if not lights or self._extra_task:
            return
        self._extra_task = self.hass.async_create_background_task(self._drive_extra(lights), f"{DOMAIN}_extra_lights")

    @callback
    def _restart_extra(self) -> None:
        # Restore the old lights first, otherwise the next snapshot would capture streamed colors.
        async def _restart() -> None:
            await self._stop_extra()
            if self.streamer.running:
                self._start_extra()

        self.hass.async_create_task(_restart())

    async def _stop_extra(self) -> None:
        if self._extra_task:
            self._extra_task.cancel()
            self._extra_task = None
        # Queued light.turn_on calls would land after the restore and override it.
        for task in self._extra_calls:
            task.cancel()
        self._extra_calls.clear()
        async with self._extra_lock:
            await asyncio.sleep(0.1)  # let calls already sent to the lights settle
            await self._restore_extra()

    async def _drive_extra(self, lights: list[dict]) -> None:
        entity_ids = [light["entity_id"] for light in lights]
        async with self._extra_lock:
            await self.hass.services.async_call(
                "scene", "create", {"scene_id": f"{DOMAIN}_restore", "snapshot_entities": entity_ids}, blocking=True
            )
            self._extra_ids = entity_ids
            self._snapshot = True
        next_due = [0.0] * len(lights)
        sent: list[tuple | None] = [None] * len(lights)
        first = [True] * len(lights)
        while True:
            now = time.monotonic()
            out = self.streamer.extra_out
            for j, light in enumerate(lights):
                if j >= len(out) or now < next_due[j]:
                    continue
                next_due[j] = now + 1 / light["update_hz"]
                rgb = out[j]
                level = max(rgb)
                bri = round(level * 255)
                color = tuple(round(v / level * 255) for v in rgb) if level > 1e-3 else (255, 255, 255)
                effect = light.get("effect")
                # Skip changes the eye won't notice; a light that smooths on its own wants fine steps.
                key = (bri, tuple(c // 2 for c in color)) if effect else (bri // 4, tuple(c // 8 for c in color))
                if key == sent[j]:
                    continue
                sent[j] = key
                # The first command fades in from the light's own state instead of jumping.
                data = {"entity_id": light["entity_id"], "transition": max(light["transition"], 0.8) if first[j] else light["transition"]}
                if effect:
                    data["transition"] = 0  # the effect does its own transitions
                    if first[j]:
                        data["effect"] = effect
                first[j] = False
                if bri < 3 and not effect:
                    service = "turn_off"
                else:  # turning off would end the effect, so such a light only gets very dim
                    service = "turn_on"
                    data.update(rgb_color=list(color), brightness=max(1, bri))
                call = self.hass.async_create_task(self.hass.services.async_call("light", service, data))
                self._extra_calls.add(call)
                call.add_done_callback(self._extra_calls.discard)
            await asyncio.sleep(0.05)

    async def _restore_extra(self) -> None:
        if self._snapshot:
            self._snapshot = False
            if ends_with_lights_off(self.streamer.stop_reason, self.lights_off_with_tv):
                await self.hass.services.async_call(
                    "light", "turn_off", {"entity_id": self._extra_ids, "transition": 0.8}, blocking=True
                )
                return
            await self.hass.services.async_call(
                "scene", "turn_on", {"entity_id": f"scene.{DOMAIN}_restore", "transition": 0.8}, blocking=True
            )
