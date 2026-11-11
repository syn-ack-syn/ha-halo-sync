"""Active preset selector (usable from automations)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity

from .entity import AmbiHueEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PresetSelect(entry, "preset")])


class PresetSelect(AmbiHueEntity, SelectEntity):
    _attr_icon = "mdi:palette"

    @property
    def options(self) -> list[str]:
        return [p["name"] for p in self._mgr.presets.values()]

    @property
    def current_option(self) -> str:
        return self._mgr.presets[self._mgr.active]["name"]

    async def async_select_option(self, option: str) -> None:
        pid = self._mgr.find_by_name(option)
        if pid:
            self._mgr.activate(pid)
