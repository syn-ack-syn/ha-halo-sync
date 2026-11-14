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


class HueAuthError(ApiError):
    """Raised when the Hue bridge rejects the application key (HTTP 401/403)."""


def _device(dev_id: str) -> dict:
    return {
        "device_name": "Home Assistant Halo Sync",
        "device_os": "Linux",
        "app_name": "halo_sync",
        "type": "native",
        "app_id": "app.halo_sync",
        "id": dev_id,
    }


def _json(r: requests.Response, what: str):
    """The decoded answer. An answer that is no JSON is an ApiError; the text is never put in the message."""
    try:
        return r.json()
    except ValueError as exc:
        raise ApiError(f"{what}: answer is no JSON (HTTP {r.status_code})") from exc


def _missing(data: dict, *fields: str) -> list[str]:
    """Names of the fields that are absent or not a non-empty string (only names, never values)."""
    return [f for f in fields if not isinstance(data.get(f), str) or not data.get(f)]


def tv_pair_request(host: str) -> dict:
    dev_id = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    r = requests.post(
        f"https://{host}:1926/6/pair/request",
        json={"scope": ["read", "write", "control"], "device": _device(dev_id)},
        verify=False,
        timeout=10,
    )
    data = _json(r, "pair request")
    if not isinstance(data, dict):
        raise ApiError(f"pair request answer is no object (HTTP {r.status_code})")
    if data.get("error_id") != "SUCCESS":
        # Only the error id: the answer can hold the auth_key.
        raise ApiError(f"pair request failed: HTTP {r.status_code}, error_id {data.get('error_id')!r}")
    # The timestamp is signed as text together with the PIN; the TV sends it as a number.
    timestamp = data.get("timestamp")
    missing = _missing(data, "auth_key")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, str)) or timestamp == "":
        missing.append("timestamp")
    if missing:
        raise ApiError(f"pair request answer lacks {', '.join(missing)}")
    return {"id": dev_id, "auth_key": data["auth_key"], "timestamp": timestamp}


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
        allow_redirects=False,
    )
    if r.status_code != 200:
        raise ApiError(f"pair grant failed: HTTP {r.status_code}")
    data = _json(r, "pair grant")
    if not isinstance(data, dict):
        raise ApiError("pair grant answer is no object")
    if data.get("error_id") != "SUCCESS":
        raise ApiError(f"pair grant failed: error_id {data.get('error_id')!r}")
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
        allow_redirects=False,
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
    )
    data = _json(r, "bridge registration")
    if not isinstance(data, list) or not data or not all(isinstance(x, dict) for x in data):
        raise ApiError(f"bridge registration answer is no list of objects (HTTP {r.status_code})")
    first = data[0]
    success = first.get("success")
    if success is not None:
        if not isinstance(success, dict):
            raise ApiError("bridge registration answer: success is no object")
        missing = _missing(success, "username", "clientkey")
        if missing:
            raise ApiError(f"bridge registration answer lacks {', '.join(missing)}")
        user = success["username"]
        auth = requests.get(
            f"https://{host}/auth/v1",
            headers={"hue-application-key": user},
            verify=False,
            timeout=10,
            allow_redirects=False,
        )
        appid = auth.headers.get("hue-application-id")
        if not appid:
            raise ApiError(f"bridge did not return hue-application-id (HTTP {auth.status_code})")
        return {"username": user, "clientkey": success["clientkey"], "appid": appid}
    error = first.get("error")
    if isinstance(error, dict) and error.get("type") == 101:
        return None
    error_type = error.get("type") if isinstance(error, dict) else None
    raise ApiError(f"bridge registration failed: error type {error_type!r}")


def hue_entertainment_areas(host: str, user: str) -> dict[str, str]:
    r = requests.get(
        f"https://{host}/clip/v2/resource/entertainment_configuration",
        headers={"hue-application-key": user},
        verify=False,
        timeout=10,
        allow_redirects=False,
    )
    if r.status_code in (401, 403):
        # Otherwise the bridge's error answer (empty "data") would read as "no entertainment areas".
        raise HueAuthError(f"bridge rejected the application key: {r.status_code}")
    if r.status_code != 200:
        raise ApiError(f"bridge answered {r.status_code} to the entertainment area request")
    answer = _json(r, "entertainment areas")
    data = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(data, list):
        raise ApiError("entertainment area answer has no data list")
    areas: dict[str, str] = {}
    for c in data:
        meta = c.get("metadata") if isinstance(c, dict) else None
        if not isinstance(meta, dict) or not isinstance(c.get("id"), str) or not isinstance(meta.get("name"), str):
            raise ApiError("entertainment area answer has an entry without id or name")
        areas[c["id"]] = meta["name"]
    return areas
