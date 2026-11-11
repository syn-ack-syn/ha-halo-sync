"""Find compatible Philips Ambilight TVs and Hue bridges on the local network (used by the config flow).

Candidates come from mDNS, then each one is probed with a request that needs no authentication:
* TV: `/6/system` (JointSpace). Compatible = HTTPS on port 1926, API 6 or newer and Ambilight reported.
* Hue bridge: `/api/config`. Compatible = a v2 bridge (BSB002 / BSB003).
Everything is local; nothing is sent to the internet.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import warnings
from typing import Any

import requests
from urllib3.exceptions import InsecureRequestWarning

_LOGGER = logging.getLogger(__name__)

# Philips Android / Google TVs show up as Google Cast and Android TV Remote devices; the philipstv_* names are what
# other Philips platforms advertise. Every candidate is verified with a JointSpace request, so extra types are harmless.
TV_SERVICES = (
    "_googlecast._tcp.local.",
    "_androidtvremote2._tcp.local.",
    "_philipstv_s_rpc._tcp.local.",
    "_philipstv_rpc._tcp.local.",
    "_philipstv_h_rpc._tcp.local.",
)
HUE_SERVICE = "_hue._tcp.local."
HUE_V2_MODELS = ("BSB002", "BSB003")
PROBE_TIMEOUT = 2.5


# ---------------------------------------------------------------------------------------- mDNS
def _ipv4(addresses: list[str]) -> list[str]:
    out = []
    for a in addresses:
        try:
            if ipaddress.ip_address(a).version == 4 and not ipaddress.ip_address(a).is_loopback:
                out.append(a)
        except ValueError:
            continue
    return out


async def browse(azc: Any, types: tuple[str, ...], wait: float = 3.0) -> list[dict]:
    """Collect mDNS services of the given types for `wait` seconds. `azc` is an AsyncZeroconf instance."""
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

    found: dict[tuple[str, str], None] = {}

    def handler(zeroconf, service_type, name, state_change):  # noqa: ANN001 - zeroconf callback signature
        if state_change is ServiceStateChange.Added:
            found[(service_type, name)] = None

    browser = AsyncServiceBrowser(azc.zeroconf, list(types), handlers=[handler])
    try:
        await asyncio.sleep(wait)
        out = []
        for stype, name in list(found):
            info = AsyncServiceInfo(stype, name)
            if not await info.async_request(azc.zeroconf, 2000):
                continue
            props = {
                (k.decode(errors="replace") if isinstance(k, bytes) else str(k)): (v.decode(errors="replace") if isinstance(v, bytes) else v)
                for k, v in (info.properties or {}).items()
            }
            out.append({"type": stype, "name": name, "addresses": _ipv4(info.parsed_addresses()), "port": info.port, "props": props})
        return out
    finally:
        await browser.async_cancel()


# ----------------------------------------------------------------------------------------- probes
def probe_tv(host: str) -> dict | None:
    """Ask a host for its JointSpace system info. None when it is not a Philips JointSpace TV."""
    for scheme, port in (("https", 1926), ("http", 1925)):
        try:
            with warnings.catch_warnings():  # the TV uses a self-signed certificate
                warnings.simplefilter("ignore", InsecureRequestWarning)
                data = requests.get(f"{scheme}://{host}:{port}/6/system", verify=False, timeout=PROBE_TIMEOUT).json()
        except (requests.RequestException, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("api_version"), dict):
            continue
        api = data["api_version"]
        major = int(api.get("Major", 0) or 0)
        ambilight = (data.get("featuring") or {}).get("jsonfeatures", {}).get("ambilight") or []
        has_ambilight = any(str(x).lower() == "ambilight" for x in ambilight)
        if scheme != "https" or port != 1926:
            ok, reason = False, "answers on HTTP port 1925 only (older or TitanOS TV)"
        elif major < 6:
            ok, reason = False, f"JointSpace API {major} is older than 6"
        elif not has_ambilight:
            ok, reason = False, "does not report Ambilight"
        else:
            ok, reason = True, ""
        return {
            "host": host,
            "name": data.get("name") or "",
            "model": data.get("model") or "",
            "platform": data.get("os_type") or "",
            "api": f"{major}.{api.get('Minor', 0)}",
            "compatible": ok,
            "reason": reason,
        }
    return None


def probe_bridge(host: str) -> dict | None:
    """Ask a host for its (unauthenticated) Hue bridge config. None when it is not a Hue bridge."""
    try:
        data = requests.get(f"http://{host}/api/config", timeout=PROBE_TIMEOUT).json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("bridgeid"):
        return None
    model = str(data.get("modelid") or "")
    ok = model in HUE_V2_MODELS
    return {
        "host": host,
        "name": data.get("name") or "Hue bridge",
        "bridge_id": str(data["bridgeid"]).upper(),
        "model": model,
        "api": str(data.get("apiversion") or ""),
        "compatible": ok,
        "reason": "" if ok else f"model {model or 'unknown'} is not a Hue Bridge v2",
    }


# ------------------------------------------------------------------------------------- discovery
async def _run(hass: Any, fn, *args):
    if hass is not None:
        return await hass.async_add_executor_job(fn, *args)
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


async def async_discover(hass: Any, azc: Any, wait: float = 3.0) -> dict[str, list[dict]]:
    """Return {"tvs": [...], "bridges": [...]}, each sorted compatible first. Never raises."""
    try:
        services = await browse(azc, TV_SERVICES + (HUE_SERVICE,), wait)
    except Exception as exc:  # noqa: BLE001 - discovery is a convenience, manual entry must always work
        _LOGGER.debug("mDNS browse failed: %s", exc)
        services = []
    tv_hosts: dict[str, dict] = {}
    bridge_hosts: dict[str, dict] = {}
    for s in services:
        for ip in s["addresses"]:
            if s["type"] == HUE_SERVICE:
                bridge_hosts.setdefault(ip, s)
            elif s["type"] == "_googlecast._tcp.local.":
                if "philips" in str(s["props"].get("md", "")).lower():  # other Cast devices are not TVs of ours
                    tv_hosts.setdefault(ip, s)
            else:
                tv_hosts.setdefault(ip, s)

    tv_results = await asyncio.gather(*[_run(hass, probe_tv, ip) for ip in tv_hosts], return_exceptions=True)
    br_results = await asyncio.gather(*[_run(hass, probe_bridge, ip) for ip in bridge_hosts], return_exceptions=True)
    tvs = []
    for ip, res in zip(tv_hosts, tv_results):
        if isinstance(res, dict):
            cast = tv_hosts[ip]["props"]
            # Prefer the friendly names the TV advertises over JointSpace's generic one.
            res["name"] = cast.get("fn") or res["name"] or "Philips TV"
            res["model"] = cast.get("md") or res["model"]
            tvs.append(res)
    bridges = [r for r in br_results if isinstance(r, dict)]
    key = lambda d: (not d["compatible"], d["name"].lower(), d["host"])  # noqa: E731
    return {"tvs": sorted(tvs, key=key), "bridges": sorted(bridges, key=key)}


# ----------------------------------------------------------------------------- presentation
def _cell(text: str) -> str:
    return str(text).replace("|", "/").replace("\n", " ").strip() or "-"


def tv_label(d: dict) -> str:
    return " · ".join(p for p in (d["name"], d["model"], d["host"]) if p)


def bridge_label(d: dict) -> str:
    return " · ".join(p for p in (d["name"], d["model"], d["host"]) if p)


def tv_table(tvs: list[dict]) -> str:
    if not tvs:
        return "_No TV found. Make sure it is switched on and on the same network, or enter its address below._"
    rows = ["| Name | Address | Details | Status |", "|---|---|---|---|"]
    for d in tvs:
        details = " ".join(x for x in (d["model"], f"API {d['api']}", d["platform"]) if x)
        status = "Compatible" if d["compatible"] else f"Not supported: {d['reason']}"
        rows.append(f"| {_cell(d['name'])} | {_cell(d['host'])} | {_cell(details)} | {_cell(status)} |")
    return "\n".join(rows)


def bridge_table(bridges: list[dict]) -> str:
    if not bridges:
        return "_No Hue bridge found. Enter its address below._"
    rows = ["| Name | Address | Details | Status |", "|---|---|---|---|"]
    for d in bridges:
        details = " ".join(x for x in (d["model"], f"API {d['api']}" if d["api"] else "", d["bridge_id"][-6:]) if x)
        status = "Compatible" if d["compatible"] else f"Not supported: {d['reason']}"
        rows.append(f"| {_cell(d['name'])} | {_cell(d['host'])} | {_cell(details)} | {_cell(status)} |")
    return "\n".join(rows)
