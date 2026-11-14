"""Config flow: pair with the TV (PIN), link the Hue bridge (button), pick the entertainment area.

Also: reconfigure (new addresses or area, pairing kept), reauth (new TV PIN or Hue key) and options.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import requests
import voluptuous as vol

from homeassistant.components import zeroconf as ha_zeroconf
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import SelectOptionDict, SelectSelector, SelectSelectorConfig, SelectSelectorMode

from . import api, discovery
from .const import (
    CONF_ADB,
    CONF_ENT_ID,
    CONF_HUE_APPID,
    CONF_HUE_CLIENTKEY,
    CONF_HUE_HOST,
    CONF_HUE_USER,
    CONF_TV_ENDPOINT,
    CONF_TV_HOST,
    CONF_TV_PASS,
    CONF_TV_USER,
    DOMAIN,
    TV_ENDPOINT,
    TV_ENDPOINTS,
)

_LOGGER = logging.getLogger(__name__)

MANUAL = "manual"
CONF_TV_CHOICE = "tv_choice"
CONF_HUE_CHOICE = "hue_choice"
CONF_RESCAN = "rescan"
# Set by the manager in the reauth data: which pairing the device rejected ("tv" or "hue").
REAUTH_TARGET = "reauth_target"

# Anything a bad or unexpected bridge answer can raise; the flow shows an error instead of failing.
_HUE_ERRORS = (api.ApiError, requests.RequestException, KeyError, ValueError, TypeError)


class AmbiHueConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._pairing: dict | None = None
        self._areas: dict[str, str] = {}
        self._found: dict[str, list[dict]] | None = None  # autodiscovered devices, filled on first display
        self._last: dict[str, Any] = {}  # what the user entered last, to refill the form after an error

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HaloSyncOptionsFlow:
        return HaloSyncOptionsFlow()

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

    # ---- shared steps of setup, reconfigure and reauth ---------------------------------------
    async def _pair_request(self) -> str | None:
        """Ask the TV to show a new PIN. Returns an error key, or None on success."""
        try:
            self._pairing = await self.hass.async_add_executor_job(api.tv_pair_request, self._data[CONF_TV_HOST])
        except (api.ApiError, requests.RequestException) as exc:
            _LOGGER.warning("TV pairing request failed: %s", exc)
            self._pairing = None
            return "tv_unreachable"
        return None

    async def _pair_grant(self, pin: str) -> str | None:
        """Finish the pairing with the PIN. A wrong or expired PIN requests a new one right away."""
        if self._pairing is None:  # the last pairing request failed, so there is nothing to grant
            return await self._pair_request() or "bad_pin"
        try:
            user, password = await self.hass.async_add_executor_job(
                api.tv_pair_grant, self._data[CONF_TV_HOST], self._pairing, pin
            )
        except (api.ApiError, requests.RequestException) as exc:
            _LOGGER.warning("TV pairing grant failed: %s", exc)
            return await self._pair_request() or "bad_pin"
        self._data[CONF_TV_USER], self._data[CONF_TV_PASS] = user, password
        return None

    async def _hue_register(self) -> str | None:
        """Create a new Hue application key. Returns an error key, or None on success."""
        try:
            key = await self.hass.async_add_executor_job(api.hue_register, self._data[CONF_HUE_HOST])
        except _HUE_ERRORS as exc:
            _LOGGER.warning("Hue registration failed: %s", exc)
            return "hue_unreachable"
        if key is None:
            return "link_button"
        self._data[CONF_HUE_USER] = key["username"]
        self._data[CONF_HUE_CLIENTKEY] = key["clientkey"]
        self._data[CONF_HUE_APPID] = key["appid"]
        return None

    async def _load_areas(self) -> str | None:
        """Read the entertainment areas into self._areas. Returns an error key, or None when there is one."""
        try:
            self._areas = await self.hass.async_add_executor_job(
                api.hue_entertainment_areas, self._data[CONF_HUE_HOST], self._data[CONF_HUE_USER]
            )
        except _HUE_ERRORS as exc:
            _LOGGER.warning("Reading the Hue entertainment areas failed: %s", exc)
            self._areas = {}
            return "hue_unreachable"
        return None if self._areas else "no_areas"

    async def _area_form(self, step_id: str, default: str | None = None, load: bool = True) -> ConfigFlowResult:
        """The area choice. Without areas: a form without fields, submitting it checks again."""
        error = await self._load_areas() if load else None
        if error:
            return self.async_show_form(step_id=step_id, data_schema=vol.Schema({}), errors={"base": error})
        if default in self._areas:
            field = vol.Required(CONF_ENT_ID, default=default)
        else:
            field = vol.Required(CONF_ENT_ID)
        return self.async_show_form(step_id=step_id, data_schema=vol.Schema({field: vol.In(self._areas)}))

    # ---- setup ------------------------------------------------------------------------------
    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        language = self.hass.config.language
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
                    self._data.update({CONF_TV_HOST: tv, CONF_HUE_HOST: hue, CONF_ADB: user_input.get(CONF_ADB, False)})
                    error = await self._pair_request()
                    if error is None:
                        return await self.async_step_tv_pin()
                    errors["base"] = error
        if self._found is None:
            await self._discover()
        tvs, bridges = self._found["tvs"], self._found["bridges"]
        ok_tvs = [d for d in tvs if d["compatible"]]
        ok_bridges = [d for d in bridges if d["compatible"]]

        def selector(devices: list[dict], label) -> SelectSelector:
            options = [SelectOptionDict(value=d["host"], label=label(d)) for d in devices]
            options.append(SelectOptionDict(value=MANUAL, label=discovery.translate(language, "manual")))
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
                    vol.Required(CONF_ADB, default=self._last.get(CONF_ADB, False)): bool,
                    vol.Optional(CONF_RESCAN, default=False): bool,
                }
            ),
            description_placeholders={
                "tv_table": discovery.tv_table(tvs, language),
                "bridge_table": discovery.bridge_table(bridges, language),
            },
            errors=errors,
        )

    async def async_step_tv_pin(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            error = await self._pair_grant(user_input["pin"].strip())
            if error is None:
                return await self.async_step_hue_link()
            errors["base"] = error
        return self.async_show_form(step_id="tv_pin", data_schema=vol.Schema({vol.Required("pin"): str}), errors=errors)

    async def async_step_hue_link(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            error = await self._hue_register()
            if error is None:
                return await self.async_step_area()
            errors["base"] = error
        return self.async_show_form(step_id="hue_link", data_schema=vol.Schema({}), errors=errors)

    async def async_step_area(self, user_input=None) -> ConfigFlowResult:
        if user_input and user_input.get(CONF_ENT_ID) in self._areas:
            self._data[CONF_ENT_ID] = user_input[CONF_ENT_ID]
            await self.async_set_unique_id(f"{self._data[CONF_TV_HOST]}_{user_input[CONF_ENT_ID]}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"Halo Sync: {self._areas[user_input[CONF_ENT_ID]]}", data=self._data)
        return await self._area_form("area")

    # ---- reconfigure: new addresses or area, the pairing and the unique ID stay ---------------
    async def async_step_reconfigure(self, user_input=None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            tv = (user_input.get(CONF_TV_HOST) or "").strip()
            hue = (user_input.get(CONF_HUE_HOST) or "").strip()
            self._data = {**entry.data, CONF_TV_HOST: tv, CONF_HUE_HOST: hue}
            if not tv:
                errors["base"] = "no_tv"
            elif not hue:
                errors["base"] = "no_bridge"
            else:
                try:
                    await self.hass.async_add_executor_job(
                        api.tv_check_auth, tv, entry.data[CONF_TV_USER], entry.data[CONF_TV_PASS]
                    )
                except api.TvAuthError as exc:
                    _LOGGER.warning("TV rejected the stored pairing: %s", exc)
                    errors["base"] = "tv_auth"
                except (api.ApiError, requests.RequestException) as exc:
                    _LOGGER.warning("TV check failed: %s", exc)
                    errors["base"] = "tv_unreachable"
                else:
                    error = await self._load_areas()
                    if error is None:
                        return await self._area_form("reconfigure_area", default=entry.data.get(CONF_ENT_ID), load=False)
                    errors["base"] = error
        shown = user_input or entry.data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TV_HOST, default=shown.get(CONF_TV_HOST, "")): str,
                    vol.Required(CONF_HUE_HOST, default=shown.get(CONF_HUE_HOST, "")): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_area(self, user_input=None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input and user_input.get(CONF_ENT_ID) in self._areas:
            ent_id = user_input[CONF_ENT_ID]
            changes: dict[str, Any] = {}
            if ent_id != entry.data.get(CONF_ENT_ID):  # the title names the area; a kept area keeps a renamed title
                changes["title"] = f"Halo Sync: {self._areas[ent_id]}"
            return self.async_update_reload_and_abort(
                entry,
                data_updates={
                    CONF_TV_HOST: self._data[CONF_TV_HOST],
                    CONF_HUE_HOST: self._data[CONF_HUE_HOST],
                    CONF_ENT_ID: ent_id,
                },
                reason="reconfigure_successful",
                **changes,
            )
        return await self._area_form("reconfigure_area", default=entry.data.get(CONF_ENT_ID))

    # ---- reauth: the TV or the bridge rejected the stored pairing ------------------------------
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._data = dict(self._get_reauth_entry().data)
        target = entry_data.get(REAUTH_TARGET)
        if target == "tv":
            return await self.async_step_reauth_tv()
        if target == "hue":
            return await self.async_step_reauth_hue_link()
        return self.async_show_menu(step_id="reauth_confirm", menu_options=["reauth_tv", "reauth_hue_link"])

    async def async_step_reauth_tv(self, user_input=None) -> ConfigFlowResult:
        """Confirm first: the reauth flow starts in the background, a PIN shown then would expire unseen."""
        errors = {}
        if user_input is not None:
            error = await self._pair_request()
            if error is None:
                return await self.async_step_reauth_tv_pin()
            errors["base"] = error
        return self.async_show_form(step_id="reauth_tv", data_schema=vol.Schema({}), errors=errors)

    async def async_step_reauth_tv_pin(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            error = await self._pair_grant(user_input["pin"].strip())
            if error is None:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={CONF_TV_USER: self._data[CONF_TV_USER], CONF_TV_PASS: self._data[CONF_TV_PASS]},
                    reason="reauth_successful",
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_tv_pin", data_schema=vol.Schema({vol.Required("pin"): str}), errors=errors
        )

    async def async_step_reauth_hue_link(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            error = await self._hue_register()
            if error is None:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_HUE_USER: self._data[CONF_HUE_USER],
                        CONF_HUE_CLIENTKEY: self._data[CONF_HUE_CLIENTKEY],
                        CONF_HUE_APPID: self._data[CONF_HUE_APPID],
                    },
                    reason="reauth_successful",
                )
            errors["base"] = error
        return self.async_show_form(step_id="reauth_hue_link", data_schema=vol.Schema({}), errors=errors)


class HaloSyncOptionsFlow(OptionsFlow):
    """ADB source sensor and the TV's Ambilight endpoint. Saving reloads the entry (listener in __init__.py)."""

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            return self.async_create_entry(data={**entry.options, **user_input})
        adb = entry.options.get(CONF_ADB, entry.data.get(CONF_ADB, False))
        endpoint = entry.options.get(CONF_TV_ENDPOINT, TV_ENDPOINT)
        if endpoint not in TV_ENDPOINTS:
            endpoint = TV_ENDPOINT
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADB, default=bool(adb)): bool,
                    vol.Required(CONF_TV_ENDPOINT, default=endpoint): SelectSelector(
                        SelectSelectorConfig(
                            options=list(TV_ENDPOINTS),
                            translation_key=CONF_TV_ENDPOINT,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )
