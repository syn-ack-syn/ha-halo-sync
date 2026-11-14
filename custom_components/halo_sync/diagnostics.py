"""Diagnostics download: entry data and options without the pairing secrets, plus the manager status."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HUE_APPID, CONF_HUE_CLIENTKEY, CONF_HUE_USER, CONF_TV_PASS, CONF_TV_USER

# The Hue application ID is the DTLS identity that goes with the client key, so it is left out as well.
TO_REDACT = {CONF_TV_USER, CONF_TV_PASS, CONF_HUE_USER, CONF_HUE_CLIENTKEY, CONF_HUE_APPID}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    mgr = getattr(entry, "runtime_data", None)  # None while the entry is not loaded
    return {
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "reader_state": str(mgr.reader.state) if mgr else None,
        "status": async_redact_data(mgr.status(), TO_REDACT) if mgr else None,
    }
