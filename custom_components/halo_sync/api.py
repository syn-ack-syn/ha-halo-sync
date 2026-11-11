"""Blocking helpers for the Philips TV (JointSpace v6) and the Hue bridge (CLIP v2).

All functions here do network I/O and must run in the executor.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import string
import warnings

import requests
from requests.auth import HTTPDigestAuth

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# Shared secret every JointSpace v6 client uses to sign the pairing PIN (published in the Philips app).
_PAIR_SECRET = base64.b64decode(
    "ZmVay1EQVFOaZhwQ4Kv81ypLAZNczV9sG4KkseXWn1NEk6cXmPKO/MCa9sryslvLCFMnNe4Z4CPXzToowvhHvA=="
)


class ApiError(Exception):
    """Raised when the TV or bridge rejects a request."""


def _device(dev_id: str) -> dict:
    return {
        "device_name": "Home Assistant Halo Sync",
        "device_os": "Linux",
        "app_name": "halo_sync",
        "type": "native",
        "app_id": "app.halo_sync",
        "id": dev_id,
    }


def tv_pair_request(host: str) -> dict:
    dev_id = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    r = requests.post(
        f"https://{host}:1926/6/pair/request",
        json={"scope": ["read", "write", "control"], "device": _device(dev_id)},
        verify=False,
        timeout=10,
    ).json()
    if r.get("error_id") != "SUCCESS":
        raise ApiError(f"pair request failed: {r}")
    return {"id": dev_id, "auth_key": r["auth_key"], "timestamp": r["timestamp"]}


def tv_pair_grant(host: str, pairing: dict, pin: str) -> tuple[str, str]:
    sig = base64.b64encode(
        hmac.new(_PAIR_SECRET, f"{pairing['timestamp']}{pin}".encode(), hashlib.sha1).hexdigest().encode()
    ).decode()
    body = {
        "auth": {"auth_AppId": "1", "pin": pin, "auth_timestamp": pairing["timestamp"], "auth_signature": sig},
        "device": _device(pairing["id"]),
    }
    r = requests.post(
        f"https://{host}:1926/6/pair/grant",
        json=body,
        verify=False,
        timeout=10,
        auth=HTTPDigestAuth(pairing["id"], pairing["auth_key"]),
    )
    if r.status_code != 200 or r.json().get("error_id") != "SUCCESS":
        raise ApiError(f"pair grant failed: {r.status_code} {r.text}")
    return pairing["id"], pairing["auth_key"]


def hue_register(host: str) -> dict | None:
    """Create an app key with an entertainment client key. None = link button not pressed."""
    r = requests.post(
        f"https://{host}/api",
        json={"devicetype": "halo_sync#homeassistant", "generateclientkey": True},
        verify=False,
        timeout=10,
    ).json()
    if r and "success" in r[0]:
        user = r[0]["success"]["username"]
        appid = requests.get(
            f"https://{host}/auth/v1", headers={"hue-application-key": user}, verify=False, timeout=10
        ).headers.get("hue-application-id")
        if not appid:
            raise ApiError("bridge did not return hue-application-id")
        return {"username": user, "clientkey": r[0]["success"]["clientkey"], "appid": appid}
    if r and r[0].get("error", {}).get("type") == 101:
        return None
    raise ApiError(f"bridge registration failed: {r}")


def hue_entertainment_areas(host: str, user: str) -> dict[str, str]:
    data = requests.get(
        f"https://{host}/clip/v2/resource/entertainment_configuration",
        headers={"hue-application-key": user},
        verify=False,
        timeout=10,
    ).json()["data"]
    return {c["id"]: c["metadata"]["name"] for c in data}
