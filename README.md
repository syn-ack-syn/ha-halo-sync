# Halo Sync

## Fork

Halo Sync is a fork of [MadDinu93/ambilight-tv-hue-sync](https://github.com/MadDinu93/ambilight-tv-hue-sync),
based on its release v0.5.7. Many thanks to MadDinu93 for the original integration, its DTLS client and the
analysis of the TV's own Ambilight+Hue behaviour. Halo Sync uses its own domain (`halo_sync`), so it can be installed
next to the original without conflicts. See [NOTICE.md](NOTICE.md) for origin and license.

## About

Home Assistant integration that streams the colours of a **Philips Ambilight Android / Google TV** to the lights of a
**Philips Hue Entertainment area**, with low latency (50 Hz) and a sidebar panel with presets.

It reads the TV's own Ambilight measurement (the colours Ambilight sees at the screen edge) and drives the
Hue lights through the Hue **Entertainment API** (DTLS streaming), instead of sending one REST command per light.
That removes the ~1 s lag of REST-based setups, which are limited by the bridge to roughly 10 commands per second.

> Not affiliated with Philips, TP Vision, Signify or Home Assistant. Philips, Ambilight and Hue are trademarks of their owners.

## Features

- Streams to a Hue Entertainment area at 50 Hz over DTLS (own pure-Python DTLS 1.2 PSK client, no extra libraries).
- **Automatic discovery** in the setup screen: compatible Philips TVs and Hue bridges on your network are listed with their names and a
  select button; entering an IP address manually still works.
- Sidebar panel with **presets** (Movie, Movie Intensive, Game, Game Intensive, Relax, Native, TV Ambilight Style) and a live editor.
- **Lights follow** (per preset): smooth blend around the light, nearest edge zone on its side, average of several zones on its side, average of the whole side, or the TV's own 10 Ambilight zones.
- Tone controls: gain, curve, min/max brightness, saturation, "Ambilight on dark content" (Off / Low / Medium / High).
- **Smoothing** with separate time constants for colour and brightness, or an experimental filter that mimics the TV's own.
- **Native preset**: reproduces the look of the TV's built-in Ambilight+Hue, including an adjustable "native delay" that emulates its slower, REST-like behaviour.
- **TV Ambilight Style** preset: follows the style selected on the TV (Standard, Cinema, Sports, Game), its Ambilight brightness and saturation.
- Optional **non-Hue lights** per preset (any `light.*` entity), driven with a rate-limited `light.turn_on` and restored to their previous state when the stream stops.
- Entities: a `switch` to start/stop the stream, a `select` for the active preset, sensors for the average screen colour and the active TV input, a button to refresh the light positions.
- A built-in **connection test** (ping the TV, time the Ambilight request, probe the Hue bridge) that shows what delay to expect from your network.
- Service `halo_sync.activate_preset` for automations.

## Screenshots

Status and presets:

<img src="https://raw.githubusercontent.com/MadDinu93/ambilight-tv-hue-sync/main/docs/images/overview.png" width="560" alt="Status bar and preset cards">

Choosing what the lights follow (here: the TV's own zones, one zone per Hue light), smoothing and dark-content handling:

<img src="https://raw.githubusercontent.com/MadDinu93/ambilight-tv-hue-sync/main/docs/images/settings.png" width="560" alt="Preset editor: lights follow, TV zone per light, smoothing">

Tuning sliders and the light positions on a 16:9 screen:

<img src="https://raw.githubusercontent.com/MadDinu93/ambilight-tv-hue-sync/main/docs/images/tuning.png" width="560" alt="Tuning sliders and light positions">

## Requirements

- A **Philips Ambilight TV running Android / Google TV** that exposes the JointSpace API over HTTPS on port 1926 (`/6/ambilight/measured`).
  Developed and tested **only** on a 2024 Philips Google TV (TA5 platform); other Android / Google TV models may work but are untested.
- **Ambilight TVs running TitanOS are untested and probably will not work as they are.** I have only had access to an Android / Google TV.
  On TitanOS the JointSpace API may be reachable on a different port or protocol (community reports mention HTTP on port 1925, see the
  [Home Assistant forum thread](https://community.home-assistant.io/t/connecting-philips-tvs-running-titan-os/861249) and the
  [Philips TV integration docs](https://www.home-assistant.io/integrations/philips_js/)), I have not verified that the Ambilight measurement
  endpoint exists there, and the optional ADB source sensor only applies to Android. The same goes for older non-Android Philips TVs.
  If the TV does not answer on port 1926 the first setup step fails with "Could not reach the TV or start pairing". Reports from TitanOS
  owners are welcome as [issues](https://github.com/MadDinu93/ambilight-tv-hue-sync/issues).
- TVs without Ambilight are **not supported** (they have no Ambilight measurement to read).
- A **Hue Bridge (v2)** with an **Entertainment area** that contains the lights you want to stream to.
- Home Assistant **2025.1** or newer (developed and tested on 2026.10; older versions are untested).
- Optional: ADB over network enabled on the TV, only used for the "TV source" sensor (HDMI input vs. apps).

## Installation

### Home Assistant Marketplace / HACS

1. Open the **Marketplace** (HACS on older Home Assistant versions), add a **custom repository**:
   `https://github.com/syn-ack-syn/ha-halo-sync` (type *Integration*).
2. Install **Halo Sync** and restart Home Assistant.

### Manual

Copy `custom_components/halo_sync` into your Home Assistant `config/custom_components` folder and restart.

## Setup

1. **Settings → Devices & services → Add integration → Halo Sync**.
2. The setup screen scans your network for a few seconds and lists the **compatible TVs and Hue bridges** it finds, with their names, so you
   can just pick them. **Switch the TV on first**, otherwise it does not answer. If something is missing or you prefer to, type the IP
   address instead (a typed address overrides the selection), or tick *Scan the network again* and submit. A **PIN** then appears on the TV;
   enter it to pair.

<img src="https://raw.githubusercontent.com/MadDinu93/ambilight-tv-hue-sync/main/docs/images/setup-discovery.png" width="400" alt="Setup screen listing the discovered TV and Hue bridge">

   Discovery uses mDNS and a few unauthenticated requests on your local network (the TV's `/6/system` and the bridge's `/api/config`);
   nothing is sent to the internet. A TV that answers JointSpace but is not supported (for example only on HTTP port 1925) is listed with the reason.
3. Press the **link button** on the Hue bridge and submit within 30 seconds.
4. Pick the **Entertainment area** to stream to. Position the lights in that area with the Hue app
   (the panel shows their positions and uses them to decide what each light follows).
5. Open **Halo Sync** in the sidebar, choose a preset and press **Start**.

## Network and delay

**A poor connection to the TV adds delay to the lights, and a wired (Ethernet) connection is the most responsive.**
The stream polls the TV's Ambilight data about 25 times a second; every slow, retried or lost request shows up as lag or stutter
in the lights. Wi-Fi, especially 2.4 GHz or a crowded network, is the usual cause. Keep Home Assistant and the Hue bridge on wired connections too.

The panel has a **Connection test** that measures this from Home Assistant in about 10 seconds:

<img src="https://raw.githubusercontent.com/MadDinu93/ambilight-tv-hue-sync/main/docs/images/connection-test.png" width="560" alt="Connection test results">

| Probe | What it measures |
|---|---|
| TV ping | Round trip to the TV (ICMP; if the system does not allow it, the time to open a TCP connection). |
| TV Ambilight read | The exact request the stream makes about 25 times a second. Its **95th percentile** is what matters for smoothness. |
| Hue bridge | Round trip to the bridge. |

The verdict is based on the TV Ambilight read and packet loss:

| Verdict | TV read, 95th percentile | Packet loss |
|---|---|---|
| Excellent | 20 ms or less | 0 % |
| Good | up to 50 ms | up to 1 % |
| Fair | up to 120 ms | up to 5 % |
| Poor | More than 120 ms | More than 5 % |

The "estimated delay added by the network" is one TV read plus half a bridge round trip. Example from the author's setup
(TV and bridge on Ethernet): TV ping 0.5 ms, TV Ambilight read 9 ms median (11 ms 95th percentile), bridge 0.7 ms, so about 9 ms.
The same TV on 2.4 GHz Wi-Fi measured roughly 280 ms ping with a 6 Mbit/s link, which is enough to make the lights visibly lag.

## Presets and settings

| Setting | What it does |
|---|---|
| Lights follow | Which screen-edge pixels each light follows (see below). |
| Smoothing | Standard (two sliders) or the experimental TV filter. |
| Native delay | 0 % = as fast as possible, 100 % = like the TV's own Ambilight+Hue (slow Hue transitions, one light updated at a time). |
| Gain / Curve | How strongly the TV's (low) measured values are boosted, and the brightness response. |
| Min / Max brightness | The range the lights use on lit scenes. |
| Saturation | Above 1 = more vivid, below 1 = more pastel. |
| Ambilight on dark content | Glow kept on near-black scenes (Off / Low / Medium / High). |
| Non-Hue lights | Extra `light.*` entities with their own position on the screen. |

### Lights follow

- **Smooth (around the light):** each light blends the whole screen edge, weighted by distance (the *Spread* slider sets how wide).
- **Nearest zone on its side:** only the single edge zone closest to the light.
- **Average of several zones on its side:** the *N* zones nearest to the light along its side.
- **Average of the whole side:** the whole left, right, top or bottom edge.
- **TV zones:** the TV's own 10 Ambilight zones. By default each light uses the zone closest to its position; you can pick a zone per light in the panel.

### Automations

```yaml
action: halo_sync.activate_preset
data:
  preset: Game
```

Switch the preset by name or id, then turn `switch.halo_sync_stream` on or off
(the exact entity id depends on the name Home Assistant registered).

## How it compares

Numbers marked as measured come from the author's own setup: a 2024 Philips Google TV on Ethernet, a Hue Bridge v2 and 6 lights.
Where I have no figure I could verify, the table says so instead of guessing.

The TV's own Ambilight+Hue is compared the way it is designed to work. Philips' firmware contains a Hue Entertainment streaming client,
so the table assumes that path works. On the tested TV it does not (see the note under the table), but the processing delay I decoded from
its firmware applies either way.

| | Halo Sync (this) | The TV's built-in Ambilight+Hue (assuming Entertainment streaming) | REST-based integrations | HDMI capture (HyperHDR / Hyperion) | Hue HDMI Sync Box |
|---|---|---|---|---|---|
| Colours come from | The TV's Ambilight measurement (about 25 reads/s) | The TV's own algorithm | The TV's Ambilight data | A capture card on the HDMI signal | The HDMI signal passing through the box |
| How lights are driven | Hue Entertainment (UDP), 50 Hz | Hue Entertainment; on the tested TV it falls back to one REST command per light† | One REST command per light | Hue Entertainment | Hue Entertainment |
| Update rate | 50 frames/s to all lights | Streaming: not measured. REST fallback on the tested TV: about 5 commands/s in total† | About 10 commands/s for the whole bridge | Capture and software dependent | Not measured |
| Lag | About 10 ms network (measured), at most 40 ms sampling and 20 ms per frame (estimate), then the smoothing you pick: preset time constants range from 40 to 1200 ms. The Native preset applies the same filter as the TV (next column) | The TV's own processing: a temporal filter of about 140 ms when the light gets brighter and about 380 ms when it gets darker (decoded from the firmware, assuming a 50 Hz internal rate), plus the stream transport (not measured) | About 1 to 1.5 s with 6 lights at 3 updates/s (observed with the Ambilight Sync add-on) | Not measured; no published figure found. Includes the grabber's own latency | Not measured; launch material claims no lag, but I found no independent figure |
| Extra hardware | None | None | None | Capture card and HDMI splitter | The box itself |
| Works with other TVs and sources | Philips Ambilight Android / Google TVs only | Philips Ambilight TVs only | Philips Ambilight TVs only | Any HDMI source | Any HDMI source |
| HDR / copy protection | Not an issue (reads Ambilight data) | Not an issue | Not an issue | Needs tone mapping and an HDCP-free path | Handled by the box |

† On the tested firmware the TV's Entertainment client is switched off and out of date (the Ambilight+Hue menu entry is hidden and the
streaming call in the firmware is a stub), so the TV falls back to REST: about 5 commands per second in total, about 0.8 per light per
second, Hue transitions of 0.5 to 1.8 s, and about 1 s or more of lag was observed. Other models or newer firmware may stream properly,
in which case the processing delay above is what remains.

This integration's **Native** preset reproduces the TV's processing (zones, gain and the temporal filter). Its *native delay* slider adds the
lag of the REST fallback: 0 % is the processing alone, 100 % is what the tested TV does.

A post on the openHAB community forum, attributed to Philips, recommends staying at about 10 REST commands per second to the lights
([discussion](https://community.openhab.org/t/hue-lights-misbehaving/59156/9)); I could not verify the original document, but it matches why
REST-based approaches queue up with several lights. The bridge-to-light radio hop adds some delay to every approach and was not measured here.

## How it works

- The TV's JointSpace endpoint `/6/ambilight/measured` returns the colours at the screen edge (a ring of 21 pixels on the tested TV).
  It is polled at about 25 Hz; the raw values are low (often 10–25 % of full scale), hence the gain.
- Each light's colour is computed from that ring, smoothed, tone-mapped and sent as an Entertainment frame (50 Hz) to the bridge.
- The Native preset and the TV filter come from analysing the behaviour of one TV firmware (its zones, gain and temporal filter),
  validated against recordings of that TV. They reproduce measured behaviour; no Philips code or binaries are included.

## Known limitations

- Philips Ambilight TVs running Android / Google TV only, and only one model/firmware has been tested. TitanOS TVs are untested and probably not compatible (see *Requirements*).
- The *TV Ambilight Style* preset reads the TV's settings menu by node id, which can differ between firmware versions; if it cannot be read the integration falls back to *Standard*.
- The *TV zones* layout assumes the 3 / 7 / 3 / 8 pixel ring of the tested TV; on a differently sized ring it falls back to the nearest zone on the light's side.
- On this firmware the TV's own Ambilight output is not exposed, so the style presets emulate it instead of copying it.
- A slow or unstable connection to the TV adds delay to the lights (see *Network and delay*); a TV that is short on memory can also answer slowly. The stream stops if the TV does not answer for 15 seconds.

## License

GNU General Public License v3.0, see [LICENSE](LICENSE).

Copyright (C) 2026 MadDinu93. This program is free software: you can redistribute it and/or modify it under the terms of
the GNU General Public License as published by the Free Software Foundation, version 3. It is distributed in the hope that
it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.
