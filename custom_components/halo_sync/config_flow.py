"""Config flow: pair with the TV (PIN), link the Hue bridge (button), pick the entertainment area."""

from __future__ import annotations

import logging
from typing import Any

import requests
import voluptuous as vol

from homeassistant.components import zeroconf as ha_zeroconf
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import SelectOptionDict, SelectSelector, SelectSelectorConfig, SelectSelectorMode

from . import api, discovery
from .const import (
    CONF_ADB,
    CONF_ENT_ID,
    CONF_HUE_APPID,
    CONF_HUE_CLIENTKEY,
    CONF_HUE_HOST,
    CONF_HUE_USER,
    CONF_TV_HOST,
    CONF_TV_PASS,
    CONF_TV_USER,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

MANUAL = "manual"
CONF_TV_CHOICE = "tv_choice"
CONF_HUE_CHOICE = "hue_choice"
CONF_RESCAN = "rescan"


class AmbiHueConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._pairing: dict | None = None
        self._areas: dict[str, str] = {}
        self._found: dict[str, list[dict]] | None = None  # autodiscovered devices, filled on first display
        self._last: dict[str, Any] = {}  # what the user entered last, to refill the form after an error

    async def _discover(self) -> None:
        try:
            azc = await ha_zeroconf.async_get_async_instance(self.hass)
            self._found = await discovery.async_discover(self.hass, azc)
        except Exception as exc:  # noqa: BLE001 - discovery is a convenience; manual entry must always work
            _LOGGER.debug("device discovery failed: %s", exc)
            self._found = {"tvs": [], "bridges": []}

    @staticmethod
    def _pick(user_input: dict, manual_key: str, choice_key: str) -> str:
        """A typed address wins over the list selection."""
        manual = (user_input.get(manual_key) or "").strip()
        choice = user_input.get(choice_key)
        return manual or ("" if choice in (None, MANUAL) else str(choice))

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            self._last = dict(user_input)
            if user_input.get(CONF_RESCAN):
                self._found = None
            else:
                tv = self._pick(user_input, CONF_TV_HOST, CONF_TV_CHOICE)
                hue = self._pick(user_input, CONF_HUE_HOST, CONF_HUE_CHOICE)
                if not tv:
                    errors["base"] = "no_tv"
                elif not hue:
                    errors["base"] = "no_bridge"
                else:
                    self._data.update({CONF_TV_HOST: tv, CONF_HUE_HOST: hue, CONF_ADB: user_input.get(CONF_ADB, True)})
                    try:
                        self._pairing = await self.hass.async_add_executor_job(api.tv_pair_request, tv)
                    except (api.ApiError, requests.RequestException) as exc:
                        _LOGGER.warning("TV pairing request failed: %s", exc)
                        errors["base"] = "tv_unreachable"
                    else:
                        return await self.async_step_tv_pin()
        if self._found is None:
            await self._discover()
        tvs, bridges = self._found["tvs"], self._found["bridges"]
        ok_tvs = [d for d in tvs if d["compatible"]]
        ok_bridges = [d for d in bridges if d["compatible"]]

        def selector(devices: list[dict], label) -> SelectSelector:
            options = [SelectOptionDict(value=d["host"], label=label(d)) for d in devices]
            options.append(SelectOptionDict(value=MANUAL, label="Enter the address manually (below)"))
            return SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST))

        def default(devices: list[dict], key: str) -> str:
            previous = self._last.get(key)
            if previous in [d["host"] for d in devices] + [MANUAL]:
                return previous
            return devices[0]["host"] if devices else MANUAL

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TV_CHOICE, default=default(ok_tvs, CONF_TV_CHOICE)): selector(ok_tvs, discovery.tv_label),
                    vol.Optional(CONF_TV_HOST, default=self._last.get(CONF_TV_HOST, "")): str,
                    vol.Required(CONF_HUE_CHOICE, default=default(ok_bridges, CONF_HUE_CHOICE)): selector(ok_bridges, discovery.bridge_label),
                    vol.Optional(CONF_HUE_HOST, default=self._last.get(CONF_HUE_HOST, "")): str,
                    vol.Required(CONF_ADB, default=self._last.get(CONF_ADB, True)): bool,
                    vol.Optional(CONF_RESCAN, default=False): bool,
                }
            ),
            description_placeholders={"tv_table": discovery.tv_table(tvs), "bridge_table": discovery.bridge_table(bridges)},
            errors=errors,
        )

    async def async_step_tv_pin(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                user, password = await self.hass.async_add_executor_job(
                    api.tv_pair_grant, self._data[CONF_TV_HOST], self._pairing, user_input["pin"].strip()
                )
            except (api.ApiError, requests.RequestException) as exc:
                _LOGGER.warning("TV pairing grant failed: %s", exc)
                errors["base"] = "bad_pin"
            else:
                self._data[CONF_TV_USER], self._data[CONF_TV_PASS] = user, password
                return await self.async_step_hue_link()
        return self.async_show_form(step_id="tv_pin", data_schema=vol.Schema({vol.Required("pin"): str}), errors=errors)

    async def async_step_hue_link(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                key = await self.hass.async_add_executor_job(api.hue_register, self._data[CONF_HUE_HOST])
            except (api.ApiError, requests.RequestException) as exc:
                _LOGGER.warning("Hue registration failed: %s", exc)
                errors["base"] = "hue_unreachable"
            else:
                if key is None:
                    errors["base"] = "link_button"
                else:
                    self._data[CONF_HUE_USER] = key["username"]
                    self._data[CONF_HUE_CLIENTKEY] = key["clientkey"]
                    self._data[CONF_HUE_APPID] = key["appid"]
                    return await self.async_step_area()
        return self.async_show_form(step_id="hue_link", data_schema=vol.Schema({}), errors=errors)

    async def async_step_area(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            self._data[CONF_ENT_ID] = user_input[CONF_ENT_ID]
            await self.async_set_unique_id(f"{self._data[CONF_TV_HOST]}_{user_input[CONF_ENT_ID]}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"Ambilight TV + Hue Sync: {self._areas[user_input[CONF_ENT_ID]]}", data=self._data)
        self._areas = await self.hass.async_add_executor_job(
            api.hue_entertainment_areas, self._data[CONF_HUE_HOST], self._data[CONF_HUE_USER]
        )
        if not self._areas:
            return self.async_abort(reason="no_areas")
        return self.async_show_form(
            step_id="area", data_schema=vol.Schema({vol.Required(CONF_ENT_ID): vol.In(self._areas)})
        )
