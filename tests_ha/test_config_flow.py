"""Config flow, reconfigure, reauth, options, diagnostics and unload against a test Home Assistant.

`api` and `discovery` are mocked, nothing goes to the network. Every form, error and abort reason the
flow returns is checked against strings.json, so a missing translation key fails here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.halo_sync import api
# The real function: conftest.py patches the module attribute for every test, after this import.
from custom_components.halo_sync import async_unload_entry as real_unload_entry
from custom_components.halo_sync.const import (
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
)
from custom_components.halo_sync.diagnostics import async_get_config_entry_diagnostics
from custom_components.halo_sync.manager import Manager

ROOT = Path(__file__).resolve().parents[1]
STRINGS = json.loads((ROOT / "custom_components" / "halo_sync" / "strings.json").read_text(encoding="utf-8"))

TV, NEW_TV = "192.0.2.10", "192.0.2.11"
HUE, NEW_HUE = "192.0.2.20", "192.0.2.21"
AREA, AREA_NAME = "area-1", "Living room"
AREAS = {AREA: AREA_NAME, "area-2": "Bedroom"}

TV_USER, TV_PASS = "tv-user-stored", "tv-pass-stored"
HUE_USER, HUE_KEY, HUE_APPID = "hue-user-stored", "00112233445566778899aabbccddeeff", "app-id-stored"
NEW_TV_USER, NEW_TV_PASS = "tv-user-new", "tv-pass-new"
NEW_HUE_USER, NEW_HUE_KEY, NEW_HUE_APPID = "hue-user-new", "ffeeddccbbaa99887766554433221100", "app-id-new"
PAIRING = {"id": "pairing-id", "auth_key": "pairing-key", "timestamp": 1, "device": {}}

ENTRY_DATA = {
    CONF_TV_HOST: TV,
    CONF_HUE_HOST: HUE,
    CONF_ADB: False,
    CONF_TV_USER: TV_USER,
    CONF_TV_PASS: TV_PASS,
    CONF_HUE_USER: HUE_USER,
    CONF_HUE_CLIENTKEY: HUE_KEY,
    CONF_HUE_APPID: HUE_APPID,
    CONF_ENT_ID: AREA,
}
SECRETS = (TV_USER, TV_PASS, HUE_USER, HUE_KEY, HUE_APPID)


def checked(result, section: str = "config"):
    """The result, after making sure each of its keys has a text in strings.json."""
    texts = STRINGS[section]
    if result["type"] is FlowResultType.FORM:
        assert result["step_id"] in texts["step"], result["step_id"]
        for error in (result.get("errors") or {}).values():
            assert error in texts["error"], error
    elif result["type"] is FlowResultType.MENU:
        assert result["step_id"] in texts["step"], result["step_id"]
        for option in result["menu_options"]:
            assert option in texts["step"][result["step_id"]]["menu_options"], option
    elif result["type"] is FlowResultType.ABORT:
        assert result["reason"] in texts["abort"], result["reason"]
    return result


@pytest.fixture
def mock_api() -> Iterator[SimpleNamespace]:
    """The network calls of the flow; by default every device answers and accepts."""
    with (
        patch.object(api, "tv_pair_request", MagicMock(return_value=PAIRING)) as pair_request,
        patch.object(api, "tv_pair_grant", MagicMock(return_value=(NEW_TV_USER, NEW_TV_PASS))) as pair_grant,
        patch.object(api, "tv_check_auth", MagicMock(return_value=None)) as check_auth,
        patch.object(
            api,
            "hue_register",
            MagicMock(return_value={"username": NEW_HUE_USER, "clientkey": NEW_HUE_KEY, "appid": NEW_HUE_APPID}),
        ) as register,
        patch.object(api, "hue_entertainment_areas", MagicMock(return_value=dict(AREAS))) as areas,
    ):
        yield SimpleNamespace(
            tv_pair_request=pair_request,
            tv_pair_grant=pair_grant,
            tv_check_auth=check_auth,
            hue_register=register,
            hue_entertainment_areas=areas,
        )


@pytest.fixture(autouse=True)
def mock_discovery() -> Iterator[AsyncMock]:
    """Autodiscovery finds nothing; addresses are entered by hand."""
    with (
        patch("homeassistant.components.zeroconf.async_get_async_instance", AsyncMock(return_value=MagicMock())),
        patch("custom_components.halo_sync.discovery.async_discover", AsyncMock(return_value={"tvs": [], "bridges": []})) as discover,
    ):
        yield discover


def stored_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title=f"Halo Sync: {AREA_NAME}", unique_id=f"{TV}_{AREA}", data=dict(ENTRY_DATA)
    )
    entry.add_to_hass(hass)
    return entry


async def start_setup(hass: HomeAssistant):
    result = checked(
        await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def configure(hass: HomeAssistant, result, user_input: dict | None = None):
    return checked(await hass.config_entries.flow.async_configure(result["flow_id"], user_input or {}))


USER_INPUT = {"tv_choice": "manual", CONF_TV_HOST: TV, "hue_choice": "manual", CONF_HUE_HOST: HUE, CONF_ADB: False}


# ---- setup -------------------------------------------------------------------------------------
async def test_setup_creates_the_entry(hass: HomeAssistant, mock_api) -> None:
    result = await configure(hass, await start_setup(hass), USER_INPUT)
    assert result["step_id"] == "tv_pin"
    mock_api.tv_pair_request.assert_called_once_with(TV)

    result = await configure(hass, result, {"pin": " 1234 "})
    assert result["step_id"] == "hue_link"
    mock_api.tv_pair_grant.assert_called_once_with(TV, PAIRING, "1234")

    result = await configure(hass, result)
    assert result["step_id"] == "area"
    mock_api.hue_register.assert_called_once_with(HUE)
    mock_api.hue_entertainment_areas.assert_called_once_with(HUE, NEW_HUE_USER)

    result = await configure(hass, result, {CONF_ENT_ID: AREA})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Halo Sync: {AREA_NAME}"
    assert result["result"].unique_id == f"{TV}_{AREA}"
    assert result["data"] == {
        CONF_TV_HOST: TV,
        CONF_HUE_HOST: HUE,
        CONF_ADB: False,
        CONF_TV_USER: NEW_TV_USER,
        CONF_TV_PASS: NEW_TV_PASS,
        CONF_HUE_USER: NEW_HUE_USER,
        CONF_HUE_CLIENTKEY: NEW_HUE_KEY,
        CONF_HUE_APPID: NEW_HUE_APPID,
        CONF_ENT_ID: AREA,
    }


async def test_unreachable_tv_shows_the_form_again(hass: HomeAssistant, mock_api) -> None:
    mock_api.tv_pair_request.side_effect = requests.ConnectionError("no route")
    result = await configure(hass, await start_setup(hass), USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "tv_unreachable"}


async def test_wrong_pin_requests_a_new_pin(hass: HomeAssistant, mock_api) -> None:
    result = await configure(hass, await start_setup(hass), USER_INPUT)
    mock_api.tv_pair_grant.side_effect = api.ApiError("pair grant failed: error_id 'INVALID_PIN'")

    result = await configure(hass, result, {"pin": "0000"})
    assert result["step_id"] == "tv_pin"
    assert result["errors"] == {"base": "bad_pin"}
    assert mock_api.tv_pair_request.call_count == 2  # the TV shows a new PIN right away

    mock_api.tv_pair_grant.side_effect = None
    result = await configure(hass, result, {"pin": "1234"})
    assert result["step_id"] == "hue_link"


async def test_link_button_not_pressed(hass: HomeAssistant, mock_api) -> None:
    mock_api.hue_register.return_value = None
    result = await configure(hass, await start_setup(hass), USER_INPUT)
    result = await configure(hass, result, {"pin": "1234"})

    result = await configure(hass, result)
    assert result["step_id"] == "hue_link"
    assert result["errors"] == {"base": "link_button"}


async def test_no_areas_shows_a_form_instead_of_aborting(hass: HomeAssistant, mock_api) -> None:
    mock_api.hue_entertainment_areas.return_value = {}
    result = await configure(hass, await start_setup(hass), USER_INPUT)
    result = await configure(hass, result, {"pin": "1234"})

    result = await configure(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "area"
    assert result["errors"] == {"base": "no_areas"}

    mock_api.hue_entertainment_areas.return_value = dict(AREAS)
    result = await configure(hass, result)  # submitting checks again
    assert result["step_id"] == "area"
    assert not result["errors"]

    result = await configure(hass, result, {CONF_ENT_ID: AREA})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_unreachable_bridge_shows_a_form(hass: HomeAssistant, mock_api) -> None:
    mock_api.hue_entertainment_areas.side_effect = api.ApiError("entertainment area answer has no data list")
    result = await configure(hass, await start_setup(hass), USER_INPUT)
    result = await configure(hass, result, {"pin": "1234"})

    result = await configure(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "area"
    assert result["errors"] == {"base": "hue_unreachable"}


async def test_rejected_key_in_setup_asks_for_the_link_button_again(hass: HomeAssistant, mock_api) -> None:
    mock_api.hue_entertainment_areas.side_effect = [api.HueAuthError("bridge rejected the application key: 403"), dict(AREAS)]
    second_key = {"username": "hue-user-second", "clientkey": "abcdef0123456789abcdef0123456789", "appid": "app-id-second"}
    result = await configure(hass, await start_setup(hass), USER_INPUT)
    result = await configure(hass, result, {"pin": "1234"})

    result = await configure(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hue_link"
    assert result["errors"] == {"base": "hue_auth"}

    mock_api.hue_register.return_value = second_key
    result = await configure(hass, result)
    assert result["step_id"] == "area"
    assert mock_api.hue_register.call_count == 2
    assert mock_api.hue_entertainment_areas.call_args.args == (HUE, "hue-user-second")

    result = await configure(hass, result, {CONF_ENT_ID: AREA})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HUE_USER] == "hue-user-second"
    assert result["data"][CONF_HUE_CLIENTKEY] == second_key["clientkey"]
    assert result["data"][CONF_HUE_APPID] == "app-id-second"


async def test_already_configured(hass: HomeAssistant, mock_api) -> None:
    stored_entry(hass)
    result = await configure(hass, await start_setup(hass), USER_INPUT)
    result = await configure(hass, result, {"pin": "1234"})
    result = await configure(hass, result)

    result = await configure(hass, result, {CONF_ENT_ID: AREA})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


# ---- reconfigure --------------------------------------------------------------------------------
async def test_reconfigure_changes_the_tv_host_and_keeps_the_entry(hass: HomeAssistant, mock_api) -> None:
    entry = stored_entry(hass)
    result = checked(await entry.start_reconfigure_flow(hass))
    assert result["step_id"] == "reconfigure"

    result = await configure(hass, result, {CONF_TV_HOST: NEW_TV, CONF_HUE_HOST: HUE})
    assert result["step_id"] == "reconfigure_area"
    mock_api.tv_check_auth.assert_called_once_with(NEW_TV, TV_USER, TV_PASS)
    mock_api.hue_entertainment_areas.assert_called_once_with(HUE, HUE_USER)

    result = await configure(hass, result, {CONF_ENT_ID: AREA})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert entry.unique_id == f"{TV}_{AREA}"
    assert entry.data == {**ENTRY_DATA, CONF_TV_HOST: NEW_TV}
    mock_api.tv_pair_request.assert_not_called()
    mock_api.hue_register.assert_not_called()


async def test_reconfigure_unreachable_new_tv(hass: HomeAssistant, mock_api) -> None:
    entry = stored_entry(hass)
    mock_api.tv_check_auth.side_effect = requests.ConnectionError("no route")
    result = await entry.start_reconfigure_flow(hass)

    result = await configure(hass, result, {CONF_TV_HOST: NEW_TV, CONF_HUE_HOST: HUE})
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "tv_unreachable"}
    assert entry.data == ENTRY_DATA


async def test_reconfigure_new_tv_that_rejects_the_pairing(hass: HomeAssistant, mock_api) -> None:
    entry = stored_entry(hass)
    mock_api.tv_check_auth.side_effect = api.TvAuthError("TV rejected the pairing credentials: 401")
    result = await entry.start_reconfigure_flow(hass)

    result = await configure(hass, result, {CONF_TV_HOST: NEW_TV, CONF_HUE_HOST: HUE})
    assert result["step_id"] == "reconfigure_tv"
    assert result["errors"] == {"base": "tv_auth"}
    mock_api.tv_pair_request.assert_not_called()  # a PIN only after the user confirmed

    result = await configure(hass, result)
    assert result["step_id"] == "reconfigure_tv_pin"
    mock_api.tv_pair_request.assert_called_once_with(NEW_TV)

    result = await configure(hass, result, {"pin": "1234"})
    assert result["step_id"] == "reconfigure_area"
    mock_api.tv_pair_grant.assert_called_once_with(NEW_TV, PAIRING, "1234")

    result = await configure(hass, result, {CONF_ENT_ID: AREA})
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {**ENTRY_DATA, CONF_TV_HOST: NEW_TV, CONF_TV_USER: NEW_TV_USER, CONF_TV_PASS: NEW_TV_PASS}


async def test_reconfigure_new_tv_and_new_bridge(hass: HomeAssistant, mock_api) -> None:
    entry = stored_entry(hass)
    mock_api.tv_check_auth.side_effect = api.TvAuthError("TV rejected the pairing credentials: 401")
    result = await entry.start_reconfigure_flow(hass)

    result = await configure(hass, result, {CONF_TV_HOST: NEW_TV, CONF_HUE_HOST: NEW_HUE})
    assert result["step_id"] == "reconfigure_tv"
    result = await configure(hass, result)
    result = await configure(hass, result, {"pin": "1234"})
    assert result["step_id"] == "reconfigure_hue_link"
    mock_api.hue_entertainment_areas.assert_not_called()

    result = await configure(hass, result)
    assert result["step_id"] == "reconfigure_area"
    result = await configure(hass, result, {CONF_ENT_ID: AREA})
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert not _stored_key_sent_to(mock_api, NEW_HUE)
    assert entry.data == {
        **ENTRY_DATA,
        CONF_TV_HOST: NEW_TV,
        CONF_TV_USER: NEW_TV_USER,
        CONF_TV_PASS: NEW_TV_PASS,
        CONF_HUE_HOST: NEW_HUE,
        CONF_HUE_USER: NEW_HUE_USER,
        CONF_HUE_CLIENTKEY: NEW_HUE_KEY,
        CONF_HUE_APPID: NEW_HUE_APPID,
    }


def _stored_key_sent_to(mock_api: SimpleNamespace, host: str) -> bool:
    return any(c.args == (host, HUE_USER) for c in mock_api.hue_entertainment_areas.call_args_list)


async def test_reconfigure_new_bridge_needs_the_link_button(hass: HomeAssistant, mock_api) -> None:
    entry = stored_entry(hass)
    result = await entry.start_reconfigure_flow(hass)

    result = await configure(hass, result, {CONF_TV_HOST: TV, CONF_HUE_HOST: NEW_HUE})
    assert result["step_id"] == "reconfigure_hue_link"
    assert not result["errors"]
    mock_api.hue_entertainment_areas.assert_not_called()

    result = await configure(hass, result)
    assert result["step_id"] == "reconfigure_area"
    mock_api.hue_register.assert_called_once_with(NEW_HUE)
    mock_api.hue_entertainment_areas.assert_called_once_with(NEW_HUE, NEW_HUE_USER)

    result = await configure(hass, result, {CONF_ENT_ID: AREA})
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert not _stored_key_sent_to(mock_api, NEW_HUE)
    assert entry.data == {
        **ENTRY_DATA,
        CONF_HUE_HOST: NEW_HUE,
        CONF_HUE_USER: NEW_HUE_USER,
        CONF_HUE_CLIENTKEY: NEW_HUE_KEY,
        CONF_HUE_APPID: NEW_HUE_APPID,
    }


async def test_reconfigure_new_bridge_link_button_not_pressed(hass: HomeAssistant, mock_api) -> None:
    entry = stored_entry(hass)
    mock_api.hue_register.return_value = None
    result = await entry.start_reconfigure_flow(hass)
    result = await configure(hass, result, {CONF_TV_HOST: TV, CONF_HUE_HOST: NEW_HUE})

    result = await configure(hass, result)
    assert result["step_id"] == "reconfigure_hue_link"
    assert result["errors"] == {"base": "link_button"}
    assert not _stored_key_sent_to(mock_api, NEW_HUE)
    assert entry.data == ENTRY_DATA


async def test_reconfigure_only_the_bridge_while_the_tv_is_off(hass: HomeAssistant, mock_api) -> None:
    entry = stored_entry(hass)
    mock_api.tv_check_auth.side_effect = requests.ConnectionError("TV is off")
    result = await entry.start_reconfigure_flow(hass)

    result = await configure(hass, result, {CONF_TV_HOST: TV, CONF_HUE_HOST: NEW_HUE})
    assert result["step_id"] == "reconfigure_hue_link"
    result = await configure(hass, result)
    result = await configure(hass, result, {CONF_ENT_ID: "area-2"})
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    mock_api.tv_check_auth.assert_not_called()
    assert entry.data[CONF_TV_HOST] == TV
    assert entry.data[CONF_HUE_HOST] == NEW_HUE
    assert entry.data[CONF_ENT_ID] == "area-2"
    assert entry.title == "Halo Sync: Bedroom"
    assert entry.unique_id == f"{TV}_{AREA}"


async def test_reconfigure_rejected_key_asks_for_the_link_button(hass: HomeAssistant, mock_api) -> None:
    entry = stored_entry(hass)
    mock_api.hue_entertainment_areas.side_effect = [api.HueAuthError("bridge rejected the application key: 403"), dict(AREAS)]
    result = await entry.start_reconfigure_flow(hass)

    result = await configure(hass, result, {CONF_TV_HOST: TV, CONF_HUE_HOST: HUE})
    assert result["step_id"] == "reconfigure_hue_link"
    assert result["errors"] == {"base": "hue_auth"}

    result = await configure(hass, result)
    assert result["step_id"] == "reconfigure_area"
    mock_api.hue_register.assert_called_once_with(HUE)
    assert mock_api.hue_entertainment_areas.call_args.args == (HUE, NEW_HUE_USER)

    result = await configure(hass, result, {CONF_ENT_ID: AREA})
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HUE_USER] == NEW_HUE_USER
    assert entry.data[CONF_HUE_CLIENTKEY] == NEW_HUE_KEY


# ---- reauth -------------------------------------------------------------------------------------
async def test_reauth_tv_stores_the_new_pairing(hass: HomeAssistant, mock_api) -> None:
    entry = stored_entry(hass)
    result = checked(await entry.start_reauth_flow(hass, data={"reauth_target": "tv"}))
    assert result["step_id"] == "reauth_tv"
    mock_api.tv_pair_request.assert_not_called()

    result = await configure(hass, result)
    assert result["step_id"] == "reauth_tv_pin"
    mock_api.tv_pair_request.assert_called_once_with(TV)

    result = await configure(hass, result, {"pin": "1234"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {**ENTRY_DATA, CONF_TV_USER: NEW_TV_USER, CONF_TV_PASS: NEW_TV_PASS}


async def test_reauth_hue_stores_the_new_key(hass: HomeAssistant, mock_api) -> None:
    entry = stored_entry(hass)
    result = checked(await entry.start_reauth_flow(hass, data={"reauth_target": "hue"}))
    assert result["step_id"] == "reauth_hue_link"

    result = await configure(hass, result)
    await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    mock_api.hue_register.assert_called_once_with(HUE)
    assert entry.data == {
        **ENTRY_DATA,
        CONF_HUE_USER: NEW_HUE_USER,
        CONF_HUE_CLIENTKEY: NEW_HUE_KEY,
        CONF_HUE_APPID: NEW_HUE_APPID,
    }


async def test_reauth_without_target_offers_both(hass: HomeAssistant, mock_api) -> None:
    entry = stored_entry(hass)
    result = checked(await entry.start_reauth_flow(hass))
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reauth_confirm"
    assert result["menu_options"] == ["reauth_tv", "reauth_hue_link"]

    result = await configure(hass, result, {"next_step_id": "reauth_tv"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_tv"
    result = await configure(hass, result)
    result = await configure(hass, result, {"pin": "1234"})
    await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TV_USER] == NEW_TV_USER


async def test_reauth_menu_offers_the_link_button(hass: HomeAssistant, mock_api) -> None:
    entry = stored_entry(hass)
    result = await entry.start_reauth_flow(hass)

    result = await configure(hass, result, {"next_step_id": "reauth_hue_link"})
    assert result["step_id"] == "reauth_hue_link"
    result = await configure(hass, result)
    await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_HUE_USER] == NEW_HUE_USER


# ---- options ------------------------------------------------------------------------------------
async def test_options_store_adb_source(hass: HomeAssistant) -> None:
    entry = stored_entry(hass)
    result = checked(await hass.config_entries.options.async_init(entry.entry_id), "options")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ADB: True, CONF_TV_ENDPOINT: "processed"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_ADB: True, CONF_TV_ENDPOINT: "processed"}


# ---- diagnostics --------------------------------------------------------------------------------
async def test_diagnostics_contain_no_secrets(hass: HomeAssistant) -> None:
    entry = stored_entry(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_ADB: True, CONF_TV_ENDPOINT: "measured"})
    status = {"running": True, "error": None, "error_code": None, "reader_state": "ok", "presets": {}}
    entry.runtime_data = SimpleNamespace(reader=SimpleNamespace(state="ok"), status=lambda: dict(status))

    diag = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(diag)
    for secret in SECRETS:
        assert secret not in dumped
    for key in (CONF_TV_USER, CONF_TV_PASS, CONF_HUE_USER, CONF_HUE_CLIENTKEY, CONF_HUE_APPID):
        assert diag["data"][key] == "**REDACTED**"
    assert diag["data"][CONF_TV_HOST] == TV
    assert diag["options"] == {CONF_ADB: True, CONF_TV_ENDPOINT: "measured"}
    assert diag["reader_state"] == "ok"
    assert diag["status"] == status


async def test_diagnostics_of_an_entry_that_is_not_loaded(hass: HomeAssistant) -> None:
    entry = stored_entry(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["status"] is None
    assert diag["reader_state"] is None
    for secret in SECRETS:
        assert secret not in json.dumps(diag)


# ---- unload -------------------------------------------------------------------------------------
@pytest.fixture
def loaded_manager(hass: HomeAssistant) -> tuple[MockConfigEntry, Manager]:
    """An entry with a real manager (nothing started: no reader thread, no stream)."""
    entry = stored_entry(hass)
    mgr = Manager(hass, entry, f"{DOMAIN}_{entry.entry_id}_update")
    mgr.async_shutdown = AsyncMock()
    entry.runtime_data = mgr
    return entry, mgr


async def test_diagnostics_of_a_real_manager_contain_no_secrets(hass: HomeAssistant, loaded_manager) -> None:
    entry, _mgr = loaded_manager
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(diag)  # must be storable as JSON
    for secret in SECRETS:
        assert secret not in dumped
    assert isinstance(diag["status"], dict) and "presets" in diag["status"]


async def test_unload_refused_by_the_platforms_keeps_the_manager_working(hass: HomeAssistant, loaded_manager) -> None:
    entry, mgr = loaded_manager
    seen = []

    async def unload_platforms(_entry, _platforms):
        seen.append(mgr._unloading)  # set while the platforms unload
        return False

    with patch.object(hass.config_entries, "async_unload_platforms", side_effect=unload_platforms):
        assert await real_unload_entry(hass, entry) is False
    assert seen == [True]
    assert mgr._unloading is False
    mgr.async_shutdown.assert_not_called()


async def test_unload_that_raises_keeps_the_manager_working(hass: HomeAssistant, loaded_manager) -> None:
    entry, mgr = loaded_manager
    with (
        patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(side_effect=RuntimeError("platform failed"))),
        pytest.raises(RuntimeError, match="platform failed"),
    ):
        await real_unload_entry(hass, entry)
    assert mgr._unloading is False
    mgr.async_shutdown.assert_not_called()
