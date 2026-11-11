"""Active TV source (via ADB), e.g. hdmi2 / app; unavailable when ADB is not connected."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity

from .entity import AmbiHueEntity


async def async_setup_entry(hass, entry, async_add_entities):
    entities = [TvColor(entry, "tv_color")]
    if entry.runtime_data.adb:
        entities.append(TvSource(entry, "tv_source"))
    async_add_entities(entities)


class TvColor(AmbiHueEntity, SensorEntity):
    """Average Ambilight color (#rrggbb, rgb_color attribute), polled once a second."""

    _attr_icon = "mdi:palette"

    @property
    def available(self) -> bool:
        return self._mgr.tv_color is not None

    @property
    def native_value(self) -> str | None:
        c = self._mgr.tv_color
        return "#{:02x}{:02x}{:02x}".format(*c) if c else None

    @property
    def extra_state_attributes(self) -> dict:
        return {"rgb_color": list(self._mgr.tv_color)} if self._mgr.tv_color else {}


class TvSource(AmbiHueEntity, SensorEntity):
    _attr_icon = "mdi:video-input-hdmi"

    @property
    def available(self) -> bool:
        return self._mgr.adb.source is not None

    @property
    def native_value(self) -> str | None:
        return self._mgr.adb.source
