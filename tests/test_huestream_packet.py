"""HueStream v2 packets as Streamer._stream builds them.

The packet is assembled inline in the stream loop, so the loop runs here once against fakes: a fake
TV session (no network), fake bridge REST calls and a fake DTLS client that records what is sent
and stops the stream after the first packets.
"""

from __future__ import annotations

import struct
import threading

import pytest

from helpers import uniform_layer
from halo_sync import streamer as streamer_mod

ENT_ID = "0a1b2c3d-0000-4000-8000-000000000001"  # synthetic, 36 characters like a Hue v2 resource id
CHANNELS = [
    {"channel_id": 0, "position": {"x": -1.0, "z": 0.0}},
    {"channel_id": 1, "position": {"x": 0.0, "z": 1.0}},
    {"channel_id": 2, "position": {"x": 1.0, "z": 0.0}},
]
HEADER_LEN = 16  # "HueStream" + version (2) + sequence + reserved (2) + color space + reserved


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, ""

    def json(self):
        return self._data


class _FakeTvSession:
    def __init__(self, layer):
        self._layer = layer
        self.auth = self.verify = None

    def get(self, url, timeout=None):
        assert url.endswith("/measured")
        return _Resp({"layer1": self._layer})


class _FakeDTLS:
    def __init__(self, host, port, identity, psk, timeout=None):
        self.sent: list[bytes] = []
        _FakeDTLS.instance = self

    def connect(self):
        pass

    def send(self, data: bytes):
        self.sent.append(bytes(data))
        if len(self.sent) >= 3:
            _FakeDTLS.stop.set()

    def close(self):
        pass


@pytest.fixture
def packets(monkeypatch, sizes_3side):
    layer = uniform_layer(sizes_3side, (255, 128, 0))
    cfg = {
        "tv_host": "tv.invalid",
        "tv_user": "user",
        "tv_pass": "pass",
        "hue_host": "bridge.invalid",
        "hue_user": "app-key",
        "hue_appid": "app-id",
        "hue_clientkey": "00" * 16,
        "entertainment_id": ENT_ID,
    }
    settings = {
        "source": "video", "dark_content": "off", "brightness": 100, "min_brightness": 0, "saturation": 1.0,
        "smoothing_color": 1, "smoothing_brightness": 1, "spread": 0.8, "gain": 1.0, "curve": 1.0,
        "zone_mode": "nearest", "native_delay": 0,
    }
    monkeypatch.setattr(streamer_mod.requests, "Session", lambda: _FakeTvSession(layer))
    monkeypatch.setattr(streamer_mod.requests, "get", lambda *a, **k: _Resp({"data": [{"channels": CHANNELS}]}))
    monkeypatch.setattr(streamer_mod.requests, "put", lambda *a, **k: _Resp({}))
    monkeypatch.setattr(streamer_mod, "DTLSClient", _FakeDTLS)

    s = streamer_mod.Streamer(cfg, settings, on_change=lambda: None)
    _FakeDTLS.stop = s._stop
    _FakeDTLS.instance = None  # never reuse the client (and packets) of a previous test
    errors: list[BaseException] = []

    def run() -> None:
        try:
            s._stream()
        except BaseException as exc:  # noqa: BLE001 - reported by the assert below
            errors.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=5)
    s._stop.set()
    assert not worker.is_alive(), "stream loop did not stop"
    assert not errors, f"stream loop failed: {errors!r}"
    assert _FakeDTLS.instance is not None, "no DTLS client was created"
    sent = _FakeDTLS.instance.sent
    assert len(sent) >= 3, f"expected at least 3 packets, got {len(sent)}"
    return sent


def test_header_and_version(packets):
    p = packets[0]
    assert p[:9] == b"HueStream"
    assert (p[9], p[10]) == (2, 0)  # API version 2.0


def test_reserved_bytes_and_rgb_color_space(packets):
    p = packets[0]
    assert p[12:14] == b"\x00\x00"
    assert p[14] == 0  # 0x00 = RGB, 0x01 = XY + brightness
    assert p[15] == 0


def test_sequence_number_increments(packets):
    seqs = [p[11] for p in packets]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))


def test_entertainment_id_is_36_ascii_chars(packets):
    p = packets[0]
    ent_start = p.index(ENT_ID.encode("ascii"))
    assert ent_start == HEADER_LEN
    ent = p[HEADER_LEN:HEADER_LEN + 36]
    assert len(ent) == 36
    assert ent.decode("ascii") == ENT_ID
    assert len(p) - HEADER_LEN - len(ent) == 7 * len(CHANNELS)


def test_seven_bytes_per_channel(packets):
    for p in packets:
        body = p[HEADER_LEN + 36:]
        assert len(body) == 7 * len(CHANNELS)


# A uniform edge of (255, 128, 0) with gain 1, curve 1, brightness 100 and min brightness 0 is
# passed through unchanged (a linear tone curve keeps colours above the noise level as they are),
# and HueStream RGB channels are 16 bit, so 8-bit values scale by 65535 / 255 = 257.
EXPECTED_16BIT = (255 * 257, 128 * 257, 0)


def test_channel_ids_and_16bit_colors(packets):
    body = packets[-1][HEADER_LEN + 36:]
    for j, ch in enumerate(CHANNELS):
        cid, r, g, b = struct.unpack(">BHHH", body[7 * j:7 * j + 7])
        assert cid == ch["channel_id"]
        for got, want in zip((r, g, b), EXPECTED_16BIT):
            assert abs(got - want) <= 1
