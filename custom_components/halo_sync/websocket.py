"""Websocket API used by the sidebar panel."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN


def _manager(hass: HomeAssistant, entry_id: str | None):
    entries = [e for e in hass.config_entries.async_entries(DOMAIN) if getattr(e, "runtime_data", None)]
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    return entries[0].runtime_data if entries else None


def _lights(hass: HomeAssistant) -> list[dict]:
    return sorted(
        ({"entity_id": s.entity_id, "name": s.attributes.get("friendly_name", s.entity_id)} for s in hass.states.async_all("light")),
        key=lambda x: x["name"].lower(),
    )


@callback
def async_register(hass: HomeAssistant) -> None:
    for cmd in (ws_get, ws_activate, ws_save, ws_delete, ws_run, ws_reset, ws_refresh_positions, ws_subscribe, ws_connection_test):
        websocket_api.async_register_command(hass, cmd)


def _reply(hass, connection, msg, mgr):
    connection.send_result(msg["id"], {**mgr.status(), "entry_id": mgr.entry.entry_id, "lights": _lights(hass)})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get", vol.Optional("entry_id"): str})
@websocket_api.async_response
async def ws_get(hass, connection, msg):
    mgr = _manager(hass, msg.get("entry_id"))
    if not mgr:
        return connection.send_error(msg["id"], "not_found", "Ambilight TV + Hue Sync is not configured")
    if not mgr.hue_channels:
        await mgr.async_refresh_channels()
    _reply(hass, connection, msg, mgr)


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/subscribe", vol.Optional("entry_id"): str}
)
@callback
def ws_subscribe(hass, connection, msg):
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    mgr = _manager(hass, msg.get("entry_id"))
    if not mgr:
        return connection.send_error(msg["id"], "not_found", "Ambilight TV + Hue Sync is not configured")

    @callback
    def forward() -> None:
        connection.send_message(websocket_api.event_message(msg["id"], mgr.status()))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, mgr.signal, forward)
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/activate", vol.Optional("entry_id"): str, vol.Required("preset"): str}
)
@callback
def ws_activate(hass, connection, msg):
    mgr = _manager(hass, msg.get("entry_id"))
    try:
        mgr.activate(msg["preset"])
    except (KeyError, AttributeError):
        return connection.send_error(msg["id"], "not_found", "unknown preset")
    _reply(hass, connection, msg, mgr)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/save",
        vol.Optional("entry_id"): str,
        vol.Optional("preset_id"): vol.Any(str, None),
        vol.Required("preset"): dict,
    }
)
@callback
def ws_save(hass, connection, msg):
    mgr = _manager(hass, msg.get("entry_id"))
    pid = mgr.save_preset(msg.get("preset_id"), msg["preset"])
    connection.send_result(msg["id"], {"preset_id": pid, **mgr.status(), "lights": _lights(hass)})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/delete", vol.Optional("entry_id"): str, vol.Required("preset_id"): str}
)
@callback
def ws_delete(hass, connection, msg):
    mgr = _manager(hass, msg.get("entry_id"))
    try:
        mgr.delete_preset(msg["preset_id"])
    except ValueError as exc:
        return connection.send_error(msg["id"], "invalid", str(exc))
    _reply(hass, connection, msg, mgr)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/refresh_positions", vol.Optional("entry_id"): str})
@websocket_api.async_response
async def ws_refresh_positions(hass, connection, msg):
    mgr = _manager(hass, msg.get("entry_id"))
    await mgr.async_refresh_channels()
    _reply(hass, connection, msg, mgr)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/reset", vol.Optional("entry_id"): str})
@callback
def ws_reset(hass, connection, msg):
    mgr = _manager(hass, msg.get("entry_id"))
    mgr.reset_defaults()
    _reply(hass, connection, msg, mgr)


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/run", vol.Optional("entry_id"): str, vol.Required("running"): bool}
)
@websocket_api.async_response
async def ws_run(hass, connection, msg):
    mgr = _manager(hass, msg.get("entry_id"))
    await (mgr.async_start() if msg["running"] else mgr.async_stop())
    _reply(hass, connection, msg, mgr)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/connection_test",
        vol.Optional("entry_id"): str,
        vol.Optional("count", default=20): vol.All(int, vol.Range(min=5, max=50)),
    }
)
@websocket_api.async_response
async def ws_connection_test(hass, connection, msg):
    mgr = _manager(hass, msg.get("entry_id"))
    if not mgr:
        return connection.send_error(msg["id"], "not_found", "Ambilight TV + Hue Sync is not configured")
    if mgr._nettest_lock.locked():
        return connection.send_error(msg["id"], "busy", "A connection test is already running")
    try:
        result = await mgr.async_connection_test(msg["count"])
    except Exception as exc:  # noqa: BLE001 - report any failure to the panel
        return connection.send_error(msg["id"], "failed", str(exc))
    connection.send_result(msg["id"], result)
