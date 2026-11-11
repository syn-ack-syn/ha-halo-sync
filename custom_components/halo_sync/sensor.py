"""Active TV source (via ADB), e.g. hdmi2 / app; unavailable when ADB is not connected."""

from __future__ import annotations

import time

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later

from .const import TV_COLOR_MIN_DELTA, TV_COLOR_MIN_INTERVAL_S
from .entity import AmbiHueEntity


async def async_setup_entry(hass, entry, async_add_entities):
    entities = [TvColor(entry, "tv_color")]
    if entry.runtime_data.adb:
        entities.append(TvSource(entry, "tv_source"))
    async_add_entities(entities)


def _visibly_different(a: tuple[int, int, int] | None, b: tuple[int, int, int] | None) -> bool:
    if a is None or b is None:
        return a is not b
    return max(abs(x - y) for x, y in zip(a, b)) > TV_COLOR_MIN_DELTA


class TvColor(AmbiHueEntity, SensorEntity):
    """Average Ambilight color (#rrggbb, rgb_color attribute).

    The manager updates the color once a second; this sensor changes its state at most every
    TV_COLOR_MIN_INTERVAL_S and only when a channel moved by more than TV_COLOR_MIN_DELTA, so it
    does not flood the recorder.
    """

    _attr_icon = "mdi:palette"
    _unrecorded_attributes = frozenset({"rgb_color"})

    def __init__(self, entry, key: str):
        super().__init__(entry, key)
        self._shown: tuple[int, int, int] | None = None
        self._shown_at: float | None = None  # time.monotonic() of the last state change
        self._recheck: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        # Not the base class subscription: most manager updates must not write this state.
        self._shown = self._mgr.tv_color
        self.async_on_remove(async_dispatcher_connect(self.hass, self._mgr.signal, self._on_update))
        self.async_on_remove(self._cancel_recheck)

    @callback
    def _cancel_recheck(self) -> None:
        if self._recheck:
            self._recheck()
            self._recheck = None

    @callback
    def _on_update(self) -> None:
        color = self._mgr.tv_color
        if not _visibly_different(color, self._shown):
            return
        now = time.monotonic()
        if self._shown_at is not None and now - self._shown_at < TV_COLOR_MIN_INTERVAL_S:
            # Look again when the interval is over, in case the color does not change any more.
            if self._recheck is None:
                self._recheck = async_call_later(
                    self.hass, TV_COLOR_MIN_INTERVAL_S - (now - self._shown_at), self._on_recheck
                )
            return
        self._cancel_recheck()
        self._shown, self._shown_at = color, now
        self.async_write_ha_state()

    @callback
    def _on_recheck(self, _now) -> None:
        self._recheck = None
        self._on_update()

    @property
    def available(self) -> bool:
        return self._shown is not None

    @property
    def native_value(self) -> str | None:
        c = self._shown
        return "#{:02x}{:02x}{:02x}".format(*c) if c else None

    @property
    def extra_state_attributes(self) -> dict:
        return {"rgb_color": list(self._shown)} if self._shown else {}


class TvSource(AmbiHueEntity, SensorEntity):
    _attr_icon = "mdi:video-input-hdmi"

    @property
    def available(self) -> bool:
        return self._mgr.adb.source is not None

    @property
    def native_value(self) -> str | None:
        return self._mgr.adb.source
