"""Manual refresh of the Hue entertainment-area light positions."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity

from .entity import AmbiHueEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([RefreshPositionsButton(entry, "refresh_positions")])


class RefreshPositionsButton(AmbiHueEntity, ButtonEntity):
    _attr_icon = "mdi:crosshairs-gps"

    async def async_press(self) -> None:
        await self._mgr.async_refresh_channels()
