"""Stable error codes, reauth on refused credentials and unique preset names (manager.py, streamer.py).

manager.py imports Home Assistant, which this suite does not install. The few names it imports
are stubbed here for the duration of this module only; the logic under test does not use them.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

from halo_sync import streamer as streamer_mod


def _ha_stubs() -> dict[str, types.ModuleType]:
    def module(name: str, **attrs) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        return mod

    class _Any:
        def __init__(self, *a, **k):
            pass

    return {
        "homeassistant": module("homeassistant"),
        "homeassistant.const": module("homeassistant.const", EVENT_HOMEASSISTANT_STOP="homeassistant_stop"),
        "homeassistant.core": module("homeassistant.core", Event=_Any, HomeAssistant=_Any, callback=lambda f: f),
        "homeassistant.helpers": module("homeassistant.helpers"),
        "homeassistant.helpers.dispatcher": module("homeassistant.helpers.dispatcher", async_dispatcher_send=lambda *a: None),
        "homeassistant.helpers.storage": module("homeassistant.helpers.storage", Store=_Any),
    }


@pytest.fixture(scope="module")
def manager_mod():
    with pytest.MonkeyPatch.context() as mp:
        try:
            import homeassistant  # noqa: F401
        except ImportError:
            for name, mod in _ha_stubs().items():
                mp.setitem(sys.modules, name, mod)
        mp.delitem(sys.modules, "halo_sync.manager", raising=False)
        mod = importlib.import_module("halo_sync.manager")
        yield mod
        sys.modules.pop("halo_sync.manager", None)  # nothing else may pick up the stubbed import
        vars(sys.modules["halo_sync"]).pop("manager", None)  # nor through the package attribute


# ---- streamer --------------------------------------------------------------
def test_error_code_for_exceptions():
    assert streamer_mod.error_code_for(streamer_mod.TvGone("TV stopped answering")) == "tv_gone"
    assert streamer_mod.error_code_for(streamer_mod.TvAuthFailed("x")) == "tv_auth"
    assert streamer_mod.error_code_for(RuntimeError("bridge refused")) == "unknown"


# ---- preset names ----------------------------------------------------------
PRESETS = {"movie": {"name": "Movie"}, "game": {"name": "Game"}}


@pytest.mark.parametrize("name", ["Game", "game", "  GAME  ", "gAmE"])
def test_duplicate_name_is_rejected_in_any_case(manager_mod, name):
    with pytest.raises(ValueError, match="^duplicate_name$"):
        manager_mod.check_preset_name(PRESETS, "movie", name)
    with pytest.raises(ValueError, match="^duplicate_name$"):
        manager_mod.check_preset_name(PRESETS, None, name)  # a new preset


@pytest.mark.parametrize("name", ["Movie", "movie", " MOVIE "])
def test_renaming_a_preset_to_its_own_name_is_allowed(manager_mod, name):
    assert manager_mod.check_preset_name(PRESETS, "movie", name) == name.strip()


@pytest.mark.parametrize("name", ["", "   ", None])
def test_empty_name_is_rejected(manager_mod, name):
    with pytest.raises(ValueError, match="^empty_name$"):
        manager_mod.check_preset_name(PRESETS, "movie", name)


def test_names_compare_at_the_stored_length(manager_mod):
    long = "x" * manager_mod.NAME_MAX
    presets = {"a": {"name": long}}
    with pytest.raises(ValueError, match="^duplicate_name$"):
        manager_mod.check_preset_name(presets, None, long + " and more")


# ---- _clean_preset ---------------------------------------------------------
def test_non_dict_extra_lights_are_dropped(manager_mod):
    raw = {"name": "X", "extra_lights": ["light.a", None, 3, {"entity_id": "light.b"}]}
    out = manager_mod._clean_preset(raw, "game")
    assert [light["entity_id"] for light in out["extra_lights"]] == ["light.b"]


def test_extra_lights_that_are_no_list_are_dropped(manager_mod):
    assert manager_mod._clean_preset({"extra_lights": {"entity_id": "light.a"}}, "game")["extra_lights"] == []
    assert manager_mod._clean_preset({"extra_lights": "light.a"}, "game")["extra_lights"] == []


def test_at_most_16_extra_lights(manager_mod):
    raw = {"extra_lights": [{"entity_id": f"light.l{i}"} for i in range(20)]}
    out = manager_mod._clean_preset(raw, "game")
    assert len(out["extra_lights"]) == manager_mod.MAX_EXTRA_LIGHTS == 16
    assert out["extra_lights"][0]["entity_id"] == "light.l0"


@pytest.mark.parametrize("zones", [["1", "2"], "3", 5])
def test_native_zones_that_are_no_dict_are_dropped(manager_mod, zones):
    assert manager_mod._clean_preset({"native_zones": zones}, "game")["native_zones"] == {}


# ---- Manager without Home Assistant -----------------------------------------
class _Entry:
    def __init__(self):
        self.data = {"tv_host": "tv.invalid", "hue_host": "bridge.invalid"}
        self.reauths: list[dict] = []

    def async_start_reauth(self, hass, context=None, data=None):
        self.reauths.append(data)


class _Hass:
    def async_create_task(self, coro, *a, **k):
        coro.close()


@pytest.fixture
def mgr(manager_mod):
    m = manager_mod.Manager.__new__(manager_mod.Manager)  # no Store, reader or streamer threads
    m.hass, m.entry = _Hass(), _Entry()
    m.presets = {pid: manager_mod._clean_preset(p, pid) for pid, p in PRESETS.items()}
    m.active = "movie"
    m.settings = {}
    m._unloading = False
    m._reauth_started = False
    m._was_running = False
    m.lights_off_with_tv = False
    m.saved = 0
    m._save = lambda: setattr(m, "saved", m.saved + 1)
    m.notify = lambda: None
    m._apply_active = lambda: None
    m._restart_extra = lambda: None
    m._maybe_resume = lambda: None
    m.reader = types.SimpleNamespace(state="ok")
    m.streamer = types.SimpleNamespace(running=False, error_code=None, end_reason=None, stop_reason=None)
    m.resume = types.SimpleNamespace(stream_started=lambda: False, stream_ended=lambda reason: False)
    return m


def test_save_rejects_a_duplicate_name(mgr):
    with pytest.raises(ValueError, match="^duplicate_name$"):
        mgr.save_preset("movie", {"name": "game"})
    assert mgr.presets["movie"]["name"] == "Movie" and mgr.saved == 0


def test_save_keeps_the_own_name_and_strips_it(mgr):
    assert mgr.save_preset("movie", {"name": " Movie "}) == "movie"
    assert mgr.presets["movie"]["name"] == "Movie"


def test_save_new_preset_gets_an_id(mgr):
    assert mgr.save_preset(None, {"name": "Night"}) == "night"
    assert mgr.presets["night"]["name"] == "Night"


@pytest.mark.parametrize("pid", ["nope", ""])
def test_save_with_an_unknown_id_is_rejected(mgr, pid):
    with pytest.raises(ValueError, match="^unknown_preset$"):
        mgr.save_preset(pid, {"name": "Night"})


def test_delete_codes(mgr):
    with pytest.raises(ValueError, match="^unknown_preset$"):
        mgr.delete_preset("nope")
    mgr.delete_preset("game")
    with pytest.raises(ValueError, match="^last_preset$"):
        mgr.delete_preset("movie")


def test_status_has_the_codes(mgr):
    mgr.streamer = types.SimpleNamespace(running=False, error="TV in standby", error_code="tv_standby", stats={}, link="connected")
    mgr.reader.state = "auth_failed"
    mgr.adb = None
    mgr.hue_channels = []
    mgr.resume = types.SimpleNamespace(enabled=True, pending=False)
    st = mgr.status()
    assert st["error"] == "TV in standby" and st["error_code"] == "tv_standby"
    assert st["reader_state"] == "auth_failed" and st["link"] == "connected"


def test_refused_tv_credentials_start_one_reauth(mgr):
    mgr.reader.state = "auth_failed"
    mgr._on_reader_change()
    mgr._on_reader_change()
    assert mgr.entry.reauths == [{**mgr.entry.data, "reauth_target": "tv"}]


def test_stream_ending_with_a_rejected_hue_key_starts_a_reauth(mgr):
    mgr._was_running = True
    mgr.streamer.error_code = "hue_key_rejected"
    mgr.streamer.end_reason = "error"
    mgr._on_streamer_change()
    assert mgr.entry.reauths == [{**mgr.entry.data, "reauth_target": "hue"}]
    mgr.reader.state = "auth_failed"
    mgr._on_reader_change()
    assert len(mgr.entry.reauths) == 1  # once per entry lifetime


def test_manual_stop_during_a_suspected_key_alert_starts_no_reauth(mgr):
    mgr._was_running = True
    mgr.streamer.error_code = "hue_key_rejected"  # plain, forgeable alerts while still streaming
    mgr.streamer.end_reason = "user"
    mgr._on_streamer_change()
    assert mgr.entry.reauths == []


def test_unchanged_name_saves_even_if_another_preset_shares_it(mgr):
    mgr.presets["game2"] = {"name": "game"}  # stored before the uniqueness check existed
    assert mgr.save_preset("game2", {"name": "game", "max_brightness": 50}) == "game2"
    assert mgr.presets["game2"]["name"] == "game"
    with pytest.raises(ValueError, match="^duplicate_name$"):
        mgr.save_preset("movie", {"name": "GAME"})


def test_other_stream_ends_start_no_reauth(mgr):
    mgr._was_running = True
    mgr.streamer.error_code = "tv_gone"
    mgr._on_streamer_change()
    mgr.reader.state = "unreachable"
    mgr._on_reader_change()
    assert mgr.entry.reauths == []


def test_no_reauth_while_unloading(mgr):
    mgr._unloading = True
    mgr.reader.state = "auth_failed"
    mgr._on_reader_change()
    assert mgr.entry.reauths == []
