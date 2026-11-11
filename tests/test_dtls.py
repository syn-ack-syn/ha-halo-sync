"""DTLS 1.2 PSK client (dtls.py): key schedule vectors, parser hardening, handshakes against a fake bridge.

How the test vectors were made
------------------------------
The expected hex strings below were computed once with the reference functions `ref_prf` and
`ref_p_sha256` in this file, which follow RFC 5246 section 5 literally and use the HMAC of
`cryptography` instead of `hashlib`/`hmac` (so they share no code with dtls.py):

    P_SHA256(secret, seed) = HMAC(secret, A(1) + seed) + HMAC(secret, A(2) + seed) + ...
    A(0) = seed, A(i) = HMAC(secret, A(i-1))
    PRF(secret, label, seed) = P_SHA256(secret, label + seed)

Inputs (all synthetic, no real bridge key):
    psk           = 00 01 02 ... 0f (16 bytes)
    client_random = 00 01 02 ... 1f
    server_random = 20 21 22 ... 3f
    premaster     = uint16(16) + 16 * 00 + uint16(16) + psk            (RFC 4279 section 2)
    master        = PRF(premaster, "master secret", client_random + server_random)[:48]
    key_block     = PRF(master, "key expansion", server_random + client_random)[:40]
                    = client_write_key (16) + server_write_key (16) + client_IV (4) + server_IV (4)
    transcript    = b"halo-sync test transcript"
    verify_data   = PRF(master, "client finished" | "server finished", SHA256(transcript))[:12]

The tests check the hard-coded values against both dtls.py and the reference functions, so a
change on either side shows up. The PRF is also checked against the widely published TLS 1.2
PRF-SHA256 vector (secret 9bbe436b..., seed a0ba9f93..., label "test label", 100 bytes) posted to
the IETF TLS list in 2009.

The fake bridge is a minimal DTLS 1.2 PSK server on 127.0.0.1 written from the RFCs with the
reference PRF; it never touches any other network.
"""

from __future__ import annotations

import hashlib
import socket
import struct
import threading
import time
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import hmac as chmac
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from halo_sync import dtls
from halo_sync.dtls import DTLSAlert, DTLSClient, DTLSError


# ---- reference key schedule (independent of dtls.py) -------------------
def _hmac(key: bytes, msg: bytes) -> bytes:
    h = chmac.HMAC(key, hashes.SHA256())
    h.update(msg)
    return h.finalize()


def ref_p_sha256(secret: bytes, seed: bytes, size: int) -> bytes:
    out, a = b"", seed
    while len(out) < size:
        a = _hmac(secret, a)
        out += _hmac(secret, a + seed)
    return out[:size]


def ref_prf(secret: bytes, label: bytes, seed: bytes, size: int) -> bytes:
    return ref_p_sha256(secret, label + seed, size)


def ref_keys(psk: bytes, cr: bytes, sr: bytes) -> tuple[bytes, bytes, bytes, bytes, bytes]:
    n = len(psk)
    pms = n.to_bytes(2, "big") + bytes(n) + n.to_bytes(2, "big") + psk
    master = ref_prf(pms, b"master secret", cr + sr, 48)
    kb = ref_prf(master, b"key expansion", sr + cr, 40)
    return master, kb[:16], kb[16:32], kb[32:36], kb[36:40]


# ---- vectors ---------------------------------------------------------------
PSK = bytes(range(16))
CR = bytes(range(0x00, 0x20))
SR = bytes(range(0x20, 0x40))
TRANSCRIPT = b"halo-sync test transcript"
PMS_HEX = "0010" + "00" * 16 + "0010" + PSK.hex()
MASTER_HEX = "deb5ea0aabb5a89d2dbe02561ada3ced3ff6626813425bddaed85718423f4edd0b9ea64c8942ddd47e38d4f3c4ca710c"
KEY_BLOCK_HEX = "565d09eb8509c48e14775b9a26c2b94733f5e05b5838c0eae8f19fd69fe61bb92a55285b889028de"
CLIENT_FIN_HEX = "0bbf6924a7d836032dae8b02"
SERVER_FIN_HEX = "48309b19f0117aff95b24c85"

EXT_SECRET = bytes.fromhex("9bbe436ba940f017b17652849a71db35")
EXT_SEED = bytes.fromhex("a0ba9f936cda311827a6f796ffd5198c")
EXT_OUT_HEX = (
    "e3f229ba727be17b8d122620557cd453c2aab21d07c3d495329b52d4e61edb5a6b301791e90d35c9c9a46b4e14baf9"
    "af0fa022f7077def17abfd3797c0564bab4fbc91666e9def9b97fce34f796789baa48082d122ee42c5a72e5a5110ff"
    "f70187347b66"
)


def test_prf_matches_published_tls12_vector():
    assert dtls._prf(EXT_SECRET, b"test label", EXT_SEED, 100).hex() == EXT_OUT_HEX
    assert ref_prf(EXT_SECRET, b"test label", EXT_SEED, 100).hex() == EXT_OUT_HEX


def test_psk_premaster_secret():
    assert dtls._psk_premaster(PSK).hex() == PMS_HEX


def test_master_secret_and_key_block_vectors():
    master = dtls._master_secret(PSK, CR, SR)
    assert master.hex() == MASTER_HEX
    assert ref_keys(PSK, CR, SR)[0].hex() == MASTER_HEX
    ck, sk, civ, siv = dtls._key_block(master, CR, SR)
    assert (ck + sk + civ + siv).hex() == KEY_BLOCK_HEX
    assert b"".join(ref_keys(PSK, CR, SR)[1:]).hex() == KEY_BLOCK_HEX


def test_finished_verify_data_vectors():
    master = bytes.fromhex(MASTER_HEX)
    th = hashlib.sha256(TRANSCRIPT).digest()
    assert dtls._verify_data(master, b"client finished", TRANSCRIPT).hex() == CLIENT_FIN_HEX
    assert dtls._verify_data(master, b"server finished", TRANSCRIPT).hex() == SERVER_FIN_HEX
    assert ref_prf(master, b"client finished", th, 12).hex() == CLIENT_FIN_HEX
    assert ref_prf(master, b"server finished", th, 12).hex() == SERVER_FIN_HEX


def test_no_wall_clock_in_dtls():
    src = Path(dtls.__file__).read_text()
    assert "time.time()" not in src


# ---- parser hardening ------------------------------------------------------
def _rec(ctype: int, payload: bytes, epoch: int = 0, seq: int = 0, version: bytes = b"\xfe\xfd") -> bytes:
    return bytes([ctype]) + version + struct.pack(">H", epoch) + seq.to_bytes(6, "big") + struct.pack(">H", len(payload)) + payload


def _hs(htype: int, body: bytes, mseq: int, frag_off: int = 0, frag_len: int | None = None) -> bytes:
    frag_len = len(body) if frag_len is None else frag_len
    return (bytes([htype]) + len(body).to_bytes(3, "big") + struct.pack(">H", mseq)
            + frag_off.to_bytes(3, "big") + frag_len.to_bytes(3, "big") + body)


@pytest.mark.parametrize("data", [b"\x16\xfe", _rec(22, b"abc")[:12], _rec(22, b"abcdef")[:-1], _rec(22, b"") + b"\x16"])
def test_truncated_records_raise_dtls_error(data):
    with pytest.raises(DTLSError):
        dtls._parse_records(data)


def test_records_are_split():
    recs = dtls._parse_records(_rec(22, b"ab") + _rec(20, b"\x01", epoch=0, seq=1) + _rec(23, b"xyz", epoch=1, seq=7))
    assert [(t, e, f) for t, _, e, _, f in recs] == [(22, 0, b"ab"), (20, 0, b"\x01"), (23, 1, b"xyz")]
    assert recs[0][1] == b"\xfe\xfd"
    assert recs[2][3] == b"\x00\x01" + (7).to_bytes(6, "big")


@pytest.mark.parametrize("payload", [b"\x02\x00\x00", _hs(2, b"x" * 10, 1)[:-1], _hs(2, b"", 1) + b"\x0e\x00"])
def test_truncated_handshakes_raise_dtls_error(payload):
    with pytest.raises(DTLSError):
        dtls._parse_handshakes(payload)


def test_fragmented_handshake_is_refused():
    body = b"y" * 40
    with pytest.raises(DTLSError, match="fragment"):
        dtls._parse_handshakes(_hs(2, body, 1, frag_off=0, frag_len=20))  # first part only
    with pytest.raises(DTLSError, match="fragment"):
        dtls._parse_handshakes(_hs(2, body, 1, frag_off=20, frag_len=20))


@pytest.mark.parametrize("body", [b"", b"\xfe\xfd" + b"r" * 32, b"\xfe\xfd" + b"r" * 32 + b"\x20" + b"s" * 4])
def test_truncated_server_hello_raises(body):
    with pytest.raises(DTLSError):
        dtls._parse_server_hello(body)


def test_server_hello_checks_suite_and_version():
    ok = b"\xfe\xfd" + SR + b"\x00" + b"\x00\xa8" + b"\x00"
    assert dtls._parse_server_hello(ok) == SR
    with pytest.raises(DTLSError, match="cipher suite"):
        dtls._parse_server_hello(b"\xfe\xfd" + SR + b"\x00" + b"\x00\x8c" + b"\x00")
    with pytest.raises(DTLSError, match="version"):
        dtls._parse_server_hello(b"\xfe\xff" + SR + b"\x00" + b"\x00\xa8" + b"\x00")


@pytest.mark.parametrize("body", [b"", b"\xfe\xff", b"\xfe\xff\x05abc"])
def test_malformed_hello_verify_raises(body):
    with pytest.raises(DTLSError):
        dtls._parse_hello_verify(body)


def test_alert_carries_code():
    alert = dtls._parse_alert(b"\x02\x28")
    assert isinstance(alert, DTLSError)
    assert (alert.level, alert.description) == (2, 40)
    assert "handshake_failure (40)" in str(alert)
    with pytest.raises(DTLSError):
        dtls._parse_alert(b"\x02")


def test_send_before_connect_is_refused():
    c = DTLSClient("127.0.0.1", 9, "id", PSK)
    try:
        with pytest.raises(DTLSError):
            c.send(b"x")
    finally:
        c.close()


# ---- fake bridge -----------------------------------------------------------
class FakeBridge:
    """Minimal DTLS 1.2 PSK server (TLS_PSK_WITH_AES_128_GCM_SHA256) on 127.0.0.1.

    Options steer misbehaviour: `cookie` sends a HelloVerifyRequest first, `drop_hellos` ignores
    that many ClientHellos, `silent` never answers, `lenient` answers even if the client's Finished
    cannot be decrypted (a bridge that does not know the key), `tamper_finished` flips a bit of
    the server verify_data, `skip_ccs` / `finished_first` break the CCS order, `fragment` splits
    the ServerHello, `truncate` sends a cut record, `alert` answers the ClientHello with that
    alert, `encrypted_alert` answers the Finished with an epoch 1 alert, `duplicate_flight` sends
    the server flight twice, `stale_hvr` sends another HelloVerifyRequest before the flight,
    `lose_first_ccs` answers the first final flight with the Finished only (CCS lost),
    `split_finished_first` sends Finished and CCS in two datagrams in the wrong order,
    `junk_epoch1` sends an undecryptable epoch 1 record between CCS and Finished, `hello_version` /
    `fin_version` / `hvr_version` change the record version of the server flight / the Finished record / the HelloVerifyRequest,
    `close_notify_after` sends an encrypted close_notify after that many application records,
    `junk_after_app` answers application data with an undecryptable record, `junk_datagram`
    sends a cut datagram before the first answer, `late_hvr` sends a HelloVerifyRequest (fe ff)
    just before CCS + Finished. The HelloVerifyRequest
    goes out with the DTLS 1.0 record version, as RFC 6347 allows. A ClientHello from a new port
    starts a new session (the stream reconnects with a new socket).
    """

    def __init__(self, psk: bytes, **opts):
        self.psk = psk
        self.opts = {"cookie": True, "ske": True, **opts}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.peer = None
        self.session_peer = None
        self.cookie = b"fake-cookie-0123"
        self.sr = bytes(range(0x40, 0x60))
        self.hellos: list[float] = []
        self.sessions = 0  # completed handshakes
        self.identity: bytes | None = None
        self.app: list[bytes] = []
        self.finished_ok: bool | None = None
        self._reset()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _reset(self) -> None:
        self.seq = {0: 0, 1: 0}
        self.mseq = 0
        self.transcript = b""
        self.flight: list[bytes] | None = None
        self.keys = None
        self.final: bytes | None = None  # our answer to the client's final flight, resent on repeats
        self.finals = 0
        self.app_here = 0

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self.sock.close()

    # records
    def _plain(self, ctype: int, payload: bytes, version: bytes = b"\xfe\xfd") -> bytes:
        seq = self.seq[0]
        self.seq[0] += 1
        return _rec(ctype, payload, 0, seq, version)

    def _sealed(self, ctype: int, payload: bytes, version: bytes = b"\xfe\xfd") -> bytes:
        seq = self.seq[1]
        self.seq[1] += 1
        seqnum = struct.pack(">H", 1) + seq.to_bytes(6, "big")
        aad = seqnum + bytes([ctype]) + b"\xfe\xfd" + struct.pack(">H", len(payload))
        explicit = b"\xa5" * 4 + seq.to_bytes(4, "big")  # any unique 8 bytes are a valid explicit nonce
        sealed = AESGCM(self.keys["sk"]).encrypt(self.keys["siv"] + explicit, payload, aad)
        return _rec(ctype, explicit + sealed, 1, seq, version)

    def _open(self, ctype: int, seqnum: bytes, frag: bytes) -> bytes:
        explicit, sealed = frag[:8], frag[8:]
        aad = seqnum + bytes([ctype]) + b"\xfe\xfd" + struct.pack(">H", len(sealed) - 16)
        return AESGCM(self.keys["ck"]).decrypt(self.keys["civ"] + explicit, sealed, aad)

    def _msg(self, htype: int, body: bytes) -> bytes:
        m = _hs(htype, body, self.mseq)
        self.mseq += 1
        return m

    def _send(self, data: bytes) -> None:
        self.sock.sendto(data, self.peer)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, self.peer = self.sock.recvfrom(4096)
            except (TimeoutError, OSError):
                continue
            if self.peer != self.session_peer:
                self._reset()
                self.session_peer = self.peer
            pos = 0
            while pos < len(data):
                ctype, epoch = data[pos], struct.unpack(">H", data[pos + 3:pos + 5])[0]
                seqnum = data[pos + 3:pos + 11]
                length = struct.unpack(">H", data[pos + 11:pos + 13])[0]
                frag = data[pos + 13:pos + 13 + length]
                pos += 13 + length
                self._on_record(ctype, epoch, seqnum, frag)

    def _on_record(self, ctype: int, epoch: int, seqnum: bytes, frag: bytes) -> None:
        o = self.opts
        if epoch == 0 and ctype == 22:
            htype = frag[0]
            if htype == 1:
                self._on_client_hello(frag)
            elif htype == 16 and self.keys is None:  # a retransmitted final flight counts once
                body = frag[12:]
                self.identity = body[2:2 + struct.unpack(">H", body[:2])[0]]
                self.transcript += frag
                cr = self.client_random
                _, ck, sk, civ, siv = ref_keys(self.psk, cr, self.sr)
                self.master = ref_keys(self.psk, cr, self.sr)[0]
                self.keys = {"ck": ck, "sk": sk, "civ": civ, "siv": siv}
        elif epoch == 1 and ctype == 22:
            if self.final is not None:  # the client repeated its final flight: our answer was lost
                self.finals += 1
                self._send(self.final if not o.get("lose_first_ccs") else self._plain(20, b"\x01") + self.final)
                return
            try:
                fin = self._open(ctype, seqnum, frag)
            except InvalidTag:
                self.finished_ok = False
                if not o.get("lenient"):
                    self._send(self._plain(21, b"\x02\x14"))  # fatal bad_record_mac
                    return
                fin = _hs(20, b"\x00" * 12, self.mseq_client_fin)
            else:
                want = ref_prf(self.master, b"client finished", hashlib.sha256(self.transcript).digest(), 12)
                self.finished_ok = fin[12:] == want
                if not self.finished_ok:
                    self._send(self._plain(21, b"\x02\x33"))  # fatal decrypt_error
                    return
            self.transcript += fin
            if o.get("encrypted_alert"):
                self._send(self._plain(20, b"\x01") + self._sealed(21, b"\x02\x50"))
                return
            vd = ref_prf(self.master, b"server finished", hashlib.sha256(self.transcript).digest(), 12)
            if o.get("tamper_finished"):
                vd = bytes([vd[0] ^ 1]) + vd[1:]
            fin_rec = self._sealed(22, _hs(20, vd, self.mseq), o.get("fin_version", b"\xfe\xfd"))
            ccs = self._plain(20, b"\x01")
            junk = _rec(22, b"\x00" * 40, 1, 99) if o.get("junk_epoch1") else b""
            self.sessions += 1
            if o.get("skip_ccs"):
                self.final = fin_rec
            elif o.get("lose_first_ccs"):
                self.final = fin_rec  # a repeat gets CCS + Finished (see above)
            elif o.get("split_finished_first"):
                self.final = fin_rec
                self._send(fin_rec)
                self._send(ccs)
                return
            elif o.get("finished_first"):
                self.final = fin_rec + ccs
            else:
                self.final = ccs + junk + fin_rec
            if o.get("late_hvr"):  # a reply to a retransmitted first ClientHello, arriving late
                self._send(self._plain(22, _hs(3, b"\xfe\xff" + bytes([len(self.cookie)]) + self.cookie, 0), b"\xfe\xff"))
            self._send(self.final)
        elif epoch == 1 and ctype == 23:
            self.app.append(self._open(ctype, seqnum, frag))
            self.app_here += 1
            if o.get("junk_after_app"):
                self._send(_rec(21, b"\x00" * 26, 1, 500) + b"\x17\xfe")
            if self.app_here == o.get("close_notify_after"):
                self._send(self._sealed(21, b"\x01\x00"))

    def _on_client_hello(self, msg: bytes) -> None:
        o = self.opts
        self.hellos.append(time.monotonic())
        if o.get("silent"):
            return
        if o.get("drop_hellos", 0) > 0:
            o["drop_hellos"] -= 1
            return
        if o.get("alert"):
            self._send(self._plain(21, bytes(o["alert"])))
            return
        if o.get("truncate"):
            self._send(_rec(22, b"\x02" * 30)[:20])
            return
        if o.get("junk_datagram") and len(self.hellos) == 1:
            self._send(b"\x16\xfe\xfd\x00")  # cut record header, then the real answer
        body = msg[12:]
        self.client_random = body[2:34]
        sid_len = body[34]
        cookie = body[36 + sid_len:36 + sid_len + body[35 + sid_len]]
        if o["cookie"] and cookie != self.cookie:
            self._send(self._plain(22, _hs(3, b"\xfe\xff" + bytes([len(self.cookie)]) + self.cookie, 0), o.get("hvr_version", b"\xfe\xff")))
            self.mseq = 1
            return
        if self.flight is None:
            self.mseq_client_fin = struct.unpack(">H", msg[4:6])[0] + 2
            self.transcript = msg  # the ClientHello the flight answers (with cookie, or the only one)
            sh = self._msg(2, b"\xfe\xfd" + self.sr + b"\x00" + b"\x00\xa8" + b"\x00")
            msgs = [sh]
            if o["ske"]:
                msgs.append(self._msg(12, b"\x00\x00"))
            msgs.append(self._msg(14, b""))
            self.transcript += b"".join(msgs)
            self.flight = msgs
        if o.get("stale_hvr"):
            self._send(self._plain(22, _hs(3, b"\xfe\xff" + b"\x05other", 0), b"\xfe\xff"))
        if o.get("fragment"):
            sh = self.flight[0]
            body = sh[12:]
            first = sh[:6] + (0).to_bytes(3, "big") + (10).to_bytes(3, "big") + body[:10]
            second = sh[:6] + (10).to_bytes(3, "big") + (len(body) - 10).to_bytes(3, "big") + body[10:]
            self._send(self._plain(22, first) + self._plain(22, second) + self._plain(22, b"".join(self.flight[1:])))
            return
        version = o.get("hello_version", b"\xfe\xfd")
        out = self._plain(22, b"".join(self.flight), version)
        self._send(out + self._plain(22, b"".join(self.flight), version) if o.get("duplicate_flight") else out)


@pytest.fixture
def bridge():
    made: list[FakeBridge] = []

    def make(psk: bytes = PSK, **opts) -> FakeBridge:
        b = FakeBridge(psk, **opts)
        made.append(b)
        return b

    yield make
    for b in made:
        b.close()


@pytest.fixture(autouse=True)
def fast_retransmit(monkeypatch):
    monkeypatch.setattr(dtls, "RETRANSMIT_S", 0.1)


def _connect(b: FakeBridge, psk: bytes = PSK, timeout: float = 3.0) -> DTLSClient:
    c = DTLSClient("127.0.0.1", b.port, "app-id", psk, timeout=timeout)
    try:
        c.connect()
    except BaseException:
        c.close()
        raise
    return c


@pytest.mark.parametrize("opts", [
    {}, {"cookie": False}, {"ske": False}, {"duplicate_flight": True}, {"stale_hvr": True},
    {"split_finished_first": True}, {"junk_epoch1": True},
])
def test_handshake_and_encrypted_application_data(bridge, opts):
    b = bridge(**opts)
    c = _connect(b)
    try:
        c.send(b"HueStream-test")
        deadline = time.monotonic() + 2
        while not b.app and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        c.close()
    assert b.finished_ok is True  # the bridge accepted our Finished: same transcript on both sides
    assert b.identity == b"app-id"
    assert b.app[0] == b"HueStream-test"


def test_wrong_psk_against_strict_bridge_fails(bridge):
    b = bridge()
    with pytest.raises(DTLSAlert) as err:
        _connect(b, psk=b"\xff" * 16)
    assert err.value.description == 20  # bad_record_mac
    assert err.value.authenticated is False  # plain epoch 0 alert
    assert b.finished_ok is False


def test_bridge_without_the_key_is_rejected(bridge):
    """A bridge that answers without knowing the PSK cannot produce a valid server Finished.

    Its Finished does not decrypt, so it is dropped like any invalid record and the handshake
    never completes; the timeout then names the likely cause.
    """
    b = bridge(psk=b"\xee" * 16, lenient=True)
    with pytest.raises(DTLSError, match="client key"):
        _connect(b)


def test_tampered_server_finished_is_rejected(bridge):
    b = bridge(tamper_finished=True)
    with pytest.raises(DTLSError, match="server finished mismatch"):
        _connect(b)


def test_finished_without_change_cipher_spec_never_completes(bridge):
    b = bridge(skip_ccs=True)
    with pytest.raises(DTLSError, match="timeout"):
        _connect(b)
    assert b.finals == dtls.FLIGHT_ATTEMPTS - 1  # we kept repeating the final flight


def test_finished_before_change_cipher_spec_in_one_datagram_waits_for_the_ccs(bridge):
    _connect(bridge(finished_first=True)).close()


def test_lost_change_cipher_spec_is_recovered_by_retransmission(bridge):
    b = bridge(lose_first_ccs=True)
    _connect(b).close()
    assert b.finals >= 1  # only the repeated final flight got CCS + Finished


def test_server_flight_with_dtls10_record_version_is_rejected(bridge):
    b = bridge(hello_version=b"\xfe\xff")  # only a HelloVerifyRequest may carry DTLS 1.0
    with pytest.raises(DTLSError, match="record version fe ff|record version feff"):
        _connect(b)


def test_server_flight_with_unknown_record_version_is_dropped(bridge):
    with pytest.raises(DTLSError, match="timeout"):
        _connect(bridge(hello_version=b"\x03\x03"))


@pytest.mark.parametrize("version", [b"\x03\x03", b"\xfe\xfc"])
def test_hello_verify_with_unknown_record_version_is_dropped(bridge, version):
    with pytest.raises(DTLSError, match="timeout"):
        _connect(bridge(hvr_version=version))


def test_finished_with_wrong_record_version_is_dropped(bridge):
    # The tag is valid (AAD uses fe fd), so only the version check keeps this record out.
    b = bridge(fin_version=b"\xfe\xff")
    with pytest.raises(DTLSError, match="timeout"):
        _connect(b)


def _wait(cond, seconds: float = 2.0) -> None:
    deadline = time.monotonic() + seconds
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_close_notify_from_the_bridge_is_raised_on_the_next_send(bridge):
    b = bridge(close_notify_after=1)
    c = _connect(b)
    try:
        c.send(b"one")
        _wait(lambda: b.app)
        time.sleep(0.05)  # let the alert arrive
        with pytest.raises(DTLSAlert) as err:
            c.send(b"two")
        assert (err.value.level, err.value.description) == (1, 0)  # warning close_notify
        assert err.value.authenticated is True  # it decrypted with the session keys
    finally:
        c.close()


def test_invalid_records_during_the_stream_are_dropped(bridge):
    b = bridge(junk_after_app=True)
    c = _connect(b)
    try:
        for i in range(3):
            c.send(b"x%d" % i)
            _wait(lambda: len(b.app) > i)
            time.sleep(0.02)
    finally:
        c.close()
    assert b.app == [b"x0", b"x1", b"x2"]


def test_fragmented_server_hello_is_rejected(bridge):
    b = bridge(fragment=True)
    with pytest.raises(DTLSError, match="fragment"):
        _connect(b)


def test_truncated_record_from_bridge_is_dropped(bridge):
    b = bridge(truncate=True)  # every answer is a cut record
    with pytest.raises(DTLSError, match="timeout"):
        _connect(b)


def test_single_broken_datagram_does_not_break_the_handshake(bridge):
    _connect(bridge(junk_datagram=True)).close()


def test_late_hello_verify_during_the_final_flight_is_dropped(bridge):
    _connect(bridge(late_hvr=True)).close()


def test_plain_alert_is_reported_with_code(bridge):
    b = bridge(alert=(2, 40))
    with pytest.raises(DTLSAlert) as err:
        _connect(b)
    assert (err.value.level, err.value.description) == (2, 40)
    assert err.value.authenticated is False


def test_encrypted_alert_is_reported_with_code(bridge):
    b = bridge(encrypted_alert=True)
    with pytest.raises(DTLSAlert) as err:
        _connect(b)
    assert err.value.description == 80  # internal_error
    assert err.value.authenticated is True


def test_lost_hello_is_retransmitted(bridge):
    b = bridge(drop_hellos=1)
    _connect(b).close()
    assert len(b.hellos) >= 3  # dropped first copy, its retransmission, the hello with cookie


def test_silent_bridge_times_out_after_three_doubling_attempts(bridge):
    b = bridge(silent=True)
    start = time.monotonic()
    with pytest.raises(DTLSError, match="timeout"):
        _connect(b, timeout=10.0)
    elapsed = time.monotonic() - start
    assert len(b.hellos) == dtls.FLIGHT_ATTEMPTS == 3
    gaps = [b.hellos[1] - b.hellos[0], b.hellos[2] - b.hellos[1]]
    assert gaps[1] > gaps[0] * 1.5  # 0.1 s, then 0.2 s
    assert 0.6 <= elapsed < 2.0  # 0.1 + 0.2 + 0.4


def test_overall_timeout_caps_the_handshake(bridge):
    b = bridge(silent=True)
    start = time.monotonic()
    with pytest.raises(DTLSError, match="timeout"):
        _connect(b, timeout=0.15)
    assert time.monotonic() - start < 0.6


class _FloodSocket:
    """A socket that always has another datagram waiting."""

    def __init__(self):
        self.reads, self.sent = 0, []

    def recv(self, size, flags=0):
        self.reads += 1
        return _rec(23, b"\x00" * 30, 1, self.reads)

    def send(self, data):
        self.sent.append(data)

    def close(self):
        pass


def test_drain_reads_at_most_16_datagrams_per_send():
    c = DTLSClient("127.0.0.1", 9, "id", PSK)
    c.sock.close()
    c.sock = flood = _FloodSocket()
    c.epoch, c.write, c.read, c.connected = 1, (bytes(16), bytes(4)), (bytes(16), bytes(4)), True
    c.send(b"frame")
    assert flood.reads == dtls.DRAIN_MAX == 16
    assert len(flood.sent) == 1
