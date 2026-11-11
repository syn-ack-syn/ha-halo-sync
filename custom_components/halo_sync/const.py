"""Constants for Halo Sync."""

DOMAIN = "halo_sync"

CONF_TV_HOST = "tv_host"
CONF_TV_USER = "tv_user"
CONF_TV_PASS = "tv_pass"
CONF_HUE_HOST = "hue_host"
CONF_HUE_USER = "hue_user"
CONF_HUE_CLIENTKEY = "hue_clientkey"
CONF_HUE_APPID = "hue_appid"
CONF_ENT_ID = "entertainment_id"
CONF_ADB = "adb_source"

# Preset tunables: key -> (min, max, step)
TUNABLES = {
    "brightness": (5, 100, 1),  # %
    "saturation": (0.5, 2.0, 0.05),  # x
    "smoothing_color": (0, 1500, 10),  # ms
    "smoothing_brightness": (0, 2000, 10),  # ms
    "spread": (0.2, 2.0, 0.05),  # how far around a light's position the screen is sampled
    "gain": (1.0, 4.0, 0.1),  # x, Ambilight measured values are low (often 20-60/255)
    "curve": (0.4, 1.0, 0.05),  # exponent on brightness; < 1 lifts dark scenes
    "min_brightness": (0, 30, 1),  # %
    "zone_count": (1, 8, 1),  # pixels averaged along the light's side in "some" zone mode
    "native_delay": (0, 100, 5),  # % of the TV's own Ambilight+Hue lag to emulate
}
TUNABLE_FALLBACK = {"zone_count": 3, "native_delay": 0}

# Which screen pixels a light follows (see zones.py).
ZONE_MODES = ("smooth", "nearest", "some", "side", "native")
# "standard" = the two smoothing sliders; "native" (experimental) = the TV's own temporal filter
# (see streamer._NativeFilter).
SMOOTHING_MODES = ("standard", "native")

# Non-Hue lights are driven with light.turn_on, so they are rate limited.
EXTRA_LIGHT_DEFAULTS = {"x": 1.0, "z": 0.0, "update_hz": 3.0, "transition": 0.3}
EXTRA_LIGHT_MAX_HZ = 10.0

# Where colors come from: raw video edges, or the TV's own Ambilight output (its selected style).
SOURCES = ("video", "tv_style")
DARK_LEVEL = 0.03  # sampled brightness (0..1) below which a light counts as "dark"
# Below this sampled brightness a colour is mostly 8-bit noise, so it fades to neutral (the TV does the
# same below 5 % after gamma, which is ~24/255 at the Native gain).
CHROMA_FULL_LEVEL = 0.095
# "Ambilight on dark content" (same Off/Low/Medium/High as the TV setting, which the TV does not
# expose, so every preset has its own): glow level in % when the screen edge is (near) black.
DARK_CONTENT_LEVELS = {"off": 0, "low": 3, "medium": 7, "high": 12}

# "TV Ambilight Style" presets: this firmware no longer exposes the TV's Ambilight LED output
# (/ambilight/processed is all zeros, /currentconfiguration always "OFF"), so the selected style
# is read from the TV settings menu and emulated on the raw video colors.
TV_MENU_STYLE = 2130968783  # parent node; data.activenode_id = active category
TV_MENU_CATEGORIES = {2130968788: "video", 2130968785: "audio", 2130968786: "color", 2130968789: "off"}
TV_MENU_VIDEO_STYLE = 2130968788  # selected_item: 1 Standard, 2 Cinema, 3 Sports, 5 Game
TV_MENU_BRIGHTNESS = 2130968780  # 0..10
TV_MENU_SATURATION = 2130968782  # -2..2
TV_STYLE_POLL_S = 2
CHANNEL_REFRESH_S = 30  # how often Hue entertainment-area light positions are re-read from the bridge
# Emulation of the TV's Follow video styles.
TV_VIDEO_STYLES = {
    1: {"name": "Standard", "smoothing_color": 150, "smoothing_brightness": 300, "gain": 2.2, "curve": 0.7, "saturation": 1.0},
    2: {"name": "Cinema", "smoothing_color": 400, "smoothing_brightness": 700, "gain": 1.8, "curve": 0.8, "saturation": 0.9},
    3: {"name": "Sports", "smoothing_color": 60, "smoothing_brightness": 120, "gain": 2.6, "curve": 0.65, "saturation": 1.25},
    5: {"name": "Game", "smoothing_color": 40, "smoothing_brightness": 90, "gain": 2.6, "curve": 0.65, "saturation": 1.2},
}
TV_STYLE_DEFAULT = {"category": "video", "style": 1, **TV_VIDEO_STYLES[1], "brightness_factor": 1.0}

DEFAULT_PRESETS = {
    "movie": {"name": "Movie", "source": "video", "dark_content": "low", "brightness": 70, "saturation": 1.0,
              "smoothing_color": 250, "smoothing_brightness": 450, "spread": 0.8, "gain": 2.0, "curve": 0.75,
              "min_brightness": 2, "extra_lights": []},
    "movie_intensive": {"name": "Movie Intensive", "source": "video", "dark_content": "low", "brightness": 100,
                        "saturation": 1.25, "smoothing_color": 150, "smoothing_brightness": 300, "spread": 0.6,
                        "gain": 2.8, "curve": 0.65, "min_brightness": 3, "extra_lights": []},
    "game": {"name": "Game", "source": "video", "dark_content": "low", "brightness": 90, "saturation": 1.0,
             "smoothing_color": 100, "smoothing_brightness": 220, "spread": 0.7, "gain": 2.2, "curve": 0.7,
             "min_brightness": 3, "extra_lights": []},
    "game_intensive": {"name": "Game Intensive", "source": "video", "dark_content": "medium", "brightness": 100,
                       "saturation": 1.3, "smoothing_color": 40, "smoothing_brightness": 90, "spread": 0.5,
                       "gain": 3.0, "curve": 0.6, "min_brightness": 5, "extra_lights": []},
    "relax": {"name": "Relax", "source": "video", "dark_content": "medium", "brightness": 45, "saturation": 0.9,
              "smoothing_color": 800, "smoothing_brightness": 1200, "spread": 1.5, "gain": 1.6, "curve": 0.8,
              "min_brightness": 5, "extra_lights": []},
    # Follows the TV's own Ambilight style (Follow video / audio / color...); only min/max brightness apply.
    # Mimics the TV's own Ambilight+Hue: its 10 edge zones, ~2.35x brightness gain, its gamma (which already deepens colours) and
    # (optionally) its slow Hue transitions. Measured from the TV firmware, see tools/ambilight_re.
    "native": {"name": "Native", "source": "video", "dark_content": "low", "brightness": 100, "saturation": 1.0,
               "smoothing_color": 160, "smoothing_brightness": 160, "spread": 0.7, "gain": 2.35, "curve": 1.0,
               "min_brightness": 5, "zone_mode": "native", "native_delay": 100, "smoothing_mode": "native", "extra_lights": []},
    "tv_style": {"name": "TV Ambilight Style", "source": "tv_style", "dark_content": "off", "brightness": 100,
                 "saturation": 1.0, "smoothing_color": 100, "smoothing_brightness": 220, "spread": 0.7,
                 "gain": 1.0, "curve": 1.0, "min_brightness": 0, "extra_lights": []},
}
DEFAULTS_VERSION = 3  # bump when a new default preset is added, so existing installs get it once
DEFAULT_ACTIVE = "game"

SEND_HZ = 50
TV_GIVEUP_S = 15

# The single TV reader (tv_reader.py). JointSpace hangs on new TLS handshakes at a high rate and the
# TV updates its Ambilight values only about every 100 ms, so one keep-alive session at 10 Hz.
TV_POLL_HZ = 10
CONF_TV_ENDPOINT = "tv_endpoint"  # entry option, falls back to TV_ENDPOINT
TV_ENDPOINTS = ("measured", "processed")
# "measured" answered in up to 1.1 s on a 2018 55PUS7373, "processed" in up to 0.2 s; "processed" is
# all zeros on some newer firmware (see TV_MENU_STYLE above), which would read as standby.
TV_ENDPOINT = "measured"
TV_REQUEST_TIMEOUT_S = 3
TV_BACKOFF_MIN_S = 0.5  # pause after a failed request, doubled per failure up to TV_BACKOFF_MAX_S
TV_BACKOFF_MAX_S = 30
TV_POWER_POLL_S = 5  # how often /powerstate is checked while the TV is on
TV_BLACK_STANDBY_S = 5  # a completely black edge for this long counts as standby
TV_STANDBY_POLL_S = 5  # poll interval while in standby
# The tv_color sensor: at most one state change per interval, and only for a visible change.
TV_COLOR_MIN_INTERVAL_S = 10
TV_COLOR_MIN_DELTA = 10  # per channel, 0..255
ADB_PORT = 5555
PANEL_URL = "halo-sync"
STATIC_URL = "/halo_sync_static"
