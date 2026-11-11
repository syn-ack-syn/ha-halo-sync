"""Halo Sync: Philips Ambilight colors -> Hue Entertainment area, low latency."""

from __future__ import annotations

from pathlib import Path

import voluptuous as vol

from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from . import websocket
from .adb_source import AdbSource
from .const import CONF_ADB, CONF_TV_HOST, DOMAIN, PANEL_URL, STATIC_URL
from .manager import Manager

PLATFORMS = [Platform.BUTTON, Platform.SWITCH, Platform.SELECT, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    websocket.async_register(hass)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), cache_headers=False)]
    )

    async def activate_preset(call: ServiceCall) -> None:
        wanted = call.data["preset"]
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            mgr: Manager = entry.runtime_data
            pid = wanted if wanted in mgr.presets else mgr.find_by_name(wanted)
            if pid is None:
                raise ServiceValidationError(f"Unknown preset: {wanted}")
            mgr.activate(pid)

    hass.services.async_register(DOMAIN, "activate_preset", activate_preset, vol.Schema({vol.Required("preset"): cv.string}))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    mgr = Manager(hass, entry, f"{DOMAIN}_{entry.entry_id}_update")
    await mgr.async_load()
    if entry.data.get(CONF_ADB, True):
        mgr.adb = AdbSource(hass, entry.data[CONF_TV_HOST], hass.config.path(".storage", f"{DOMAIN}_adbkey"), lambda _s: mgr.notify())
        mgr.adb.start()
    entry.runtime_data = mgr
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    hass.async_create_task(mgr.async_refresh_channels())
    mgr.start_color_poll()

    if PANEL_URL not in hass.data.get("frontend_panels", {}):
        await panel_custom.async_register_panel(
            hass,
            webcomponent_name="halo-sync-panel",
            frontend_url_path=PANEL_URL,
            module_url=f"{STATIC_URL}/panel.js?v=20",
            sidebar_title="Halo Sync",
            sidebar_icon="mdi:television-ambient-light",
            require_admin=True,
            config={"entry_id": entry.entry_id},
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    mgr: Manager = entry.runtime_data
    await mgr.async_shutdown()
    if mgr.adb:
        await mgr.adb.stop()
    if not [e for e in hass.config_entries.async_loaded_entries(DOMAIN) if e.entry_id != entry.entry_id]:
        from homeassistant.components import frontend

        frontend.async_remove_panel(hass, PANEL_URL)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
