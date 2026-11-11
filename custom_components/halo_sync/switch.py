"""Stream on/off switch."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity

from .entity import AmbiHueEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([StreamSwitch(entry, "stream")])


class StreamSwitch(AmbiHueEntity, SwitchEntity):
    _attr_icon = "mdi:television-ambient-light"

    @property
    def is_on(self) -> bool:
        return self._mgr.streamer.running

    @property
    def extra_state_attributes(self) -> dict:
        s = self._mgr.streamer
        return {"preset": self._mgr.presets[self._mgr.active]["name"], "last_error": s.error, **s.stats}

    async def async_turn_on(self, **kwargs) -> None:
        await self._mgr.async_start()

    async def async_turn_off(self, **kwargs) -> None:
        await self._mgr.async_stop()
