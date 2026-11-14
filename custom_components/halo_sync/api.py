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


class TvAuthError(ApiError):
    """Raised when the TV rejects the stored pairing credentials (HTTP 401/403)."""


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
    if not isinstance(r, dict) or r.get("error_id") != "SUCCESS":
        raise ApiError(f"pair request failed: {r}")
    if "auth_key" not in r or "timestamp" not in r:
        raise ApiError(f"pair request answer has no auth_key or timestamp: {r}")
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


def tv_check_auth(host: str, user: str, password: str) -> None:
    """Authenticated read with the stored pairing, like the TV reader (`/6/powerstate`, answers in standby too).

    Raises TvAuthError when the TV rejects the credentials, ApiError for any other unexpected answer.
    """
    r = requests.get(
        f"https://{host}:1926/6/powerstate",
        verify=False,
        timeout=10,
        auth=HTTPDigestAuth(user, password),
    )
    if r.status_code in (401, 403):
        raise TvAuthError(f"TV rejected the pairing credentials: {r.status_code}")
    if r.status_code != 200:
        raise ApiError(f"TV answered {r.status_code} to an authenticated request")


def hue_register(host: str) -> dict | None:
    """Create an app key with an entertainment client key. None = link button not pressed."""
    r = requests.post(
        f"https://{host}/api",
        json={"devicetype": "halo_sync#homeassistant", "generateclientkey": True},
        verify=False,
        timeout=10,
    ).json()
    if not isinstance(r, list) or not all(isinstance(x, dict) for x in r):
        raise ApiError(f"bridge registration answer is not a list: {r}")
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
    r = requests.get(
        f"https://{host}/clip/v2/resource/entertainment_configuration",
        headers={"hue-application-key": user},
        verify=False,
        timeout=10,
    )
    if r.status_code in (401, 403):
        # Otherwise the bridge's error answer (empty "data") would read as "no entertainment areas".
        raise ApiError(f"bridge rejected the application key: {r.status_code}")
    data = r.json()["data"]
    return {c["id"]: c["metadata"]["name"] for c in data}
