"""Connection quality test: what network delay can the lights expect?

Three probes, all measured from Home Assistant:
* a ping to the TV (ICMP when `icmplib` is available, otherwise the time to open a TCP connection),
* the TV's `/ambilight/measured` endpoint, the exact request the stream polls ~25 times a second,
* a TCP connection to the Hue bridge (the stream itself is UDP, but the round trip is the same path).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time

import requests
from requests.auth import HTTPDigestAuth

_LOGGER = logging.getLogger(__name__)

PING_INTERVAL_S = 0.2
READ_INTERVAL_S = 0.1


def summarise(samples: list[float | None]) -> dict:
    """min / median / p95 / max / jitter in ms plus packet loss. None = lost or failed."""
    sent = len(samples)
    ok = [s for s in samples if s is not None]
    out: dict = {"sent": sent, "received": len(ok), "loss_pct": round(100 * (sent - len(ok)) / sent, 1) if sent else 100.0}
    if ok:
        s = sorted(ok)
        diffs = [abs(a - b) for a, b in zip(ok, ok[1:])]
        out.update(
            min=round(s[0], 1),
            median=round(s[len(s) // 2], 1),
            p95=round(s[min(len(s) - 1, round(0.95 * (len(s) - 1)))], 1),
            max=round(s[-1], 1),
            jitter=round(sum(diffs) / len(diffs), 1) if diffs else 0.0,
        )
    return out


def rate(tv_read: dict, tv_ping: dict | None = None) -> str:
    """excellent / good / fair / poor / unreachable, judged on the request the stream really makes."""
    if not tv_read.get("received"):
        return "unreachable"
    loss = max(tv_read["loss_pct"], (tv_ping or {}).get("loss_pct", 0.0))
    p95 = tv_read["p95"]
    if loss == 0 and p95 <= 20:
        return "excellent"
    if loss <= 1 and p95 <= 50:
        return "good"
    if loss <= 5 and p95 <= 120:
        return "fair"
    return "poor"


def added_delay_ms(tv_read: dict, bridge: dict | None) -> float | None:
    """Rough delay the network adds before a colour reaches the bridge: one TV read plus half a bridge round trip."""
    if not tv_read.get("received"):
        return None
    extra = (bridge["median"] / 2) if bridge and bridge.get("received") else 0.0
    return round(tv_read["median"] + extra, 1)


def _tcp_times(host: str, port: int, count: int) -> list[float | None]:
    out: list[float | None] = []
    for _ in range(count):
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=1):
                pass
            out.append((time.perf_counter() - t0) * 1000)
        except OSError:
            out.append(None)
        time.sleep(PING_INTERVAL_S)
    return out


def _read_times(host: str, user: str, password: str, count: int) -> list[float | None]:
    session = requests.Session()
    session.auth, session.verify = HTTPDigestAuth(user, password), False
    url = f"https://{host}:1926/6/ambilight/measured"
    out: list[float | None] = []
    try:
        session.get(url, timeout=3)  # warm-up: TLS handshake + digest negotiation are not part of a steady-state read
    except requests.RequestException:
        pass
    for _ in range(count):
        t0 = time.perf_counter()
        try:
            session.get(url, timeout=2).json()
            out.append((time.perf_counter() - t0) * 1000)
        except (requests.RequestException, ValueError):
            out.append(None)
        time.sleep(READ_INTERVAL_S)
    return out


async def _icmp(host: str, count: int) -> list[float | None] | None:
    try:
        from icmplib import async_ping
    except ImportError:
        return None
    for privileged in (False, True):
        try:
            res = await async_ping(host, count=count, interval=PING_INTERVAL_S, timeout=1, privileged=privileged)
        except Exception:  # noqa: BLE001 - no raw-socket permission etc.; try the other mode, then fall back to TCP
            continue
        rtts = [float(r) for r in res.rtts]
        return rtts + [None] * max(0, res.packets_sent - len(rtts))
    return None


async def run(hass, tv_host: str, tv_user: str, tv_pass: str, bridge_host: str, count: int = 20) -> dict:
    count = max(5, min(50, int(count)))
    icmp = await _icmp(tv_host, count)
    method = "icmp"
    if icmp is None:
        method = "tcp"
        icmp = await hass.async_add_executor_job(_tcp_times, tv_host, 1926, count)
    reads = await hass.async_add_executor_job(_read_times, tv_host, tv_user, tv_pass, count)
    bridge = await hass.async_add_executor_job(_tcp_times, bridge_host, 443, count)
    tv_ping, tv_read, br = summarise(icmp), summarise(reads), summarise(bridge)
    return {
        "count": count,
        "tv_ping": {**tv_ping, "method": method},
        "tv_read": tv_read,
        "bridge": br,
        "rating": rate(tv_read, tv_ping),
        "added_delay_ms": added_delay_ms(tv_read, br),
        "finished": time.time(),
    }
