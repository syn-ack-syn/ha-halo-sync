"""Minimal DTLS 1.2 client for TLS_PSK_WITH_AES_128_GCM_SHA256 (Hue Entertainment).

Pure Python on top of `cryptography`; implements only what the Hue bridge needs:
cookie exchange, PSK key exchange, AES-128-GCM records, application data out.

References: RFC 6347 (DTLS 1.2), RFC 5246 (TLS 1.2 PRF, Finished), RFC 4279 (PSK premaster
secret), RFC 5288 / RFC 5487 (AES-GCM with PSK, cipher suite 0x00A8).

The handshake transcript (RFC 6347 section 4.2.1 and 4.2.6) holds every handshake message in its
DTLS form (12-byte header with fragment_offset 0 and fragment_length = length), in message_seq
order, starting with the ClientHello the server answered with its ServerHello. The cookie-less
ClientHello and the HelloVerifyRequest are left out when the bridge asked for a cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import struct
import time
from collections.abc import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = b"\xfe\xfd"  # DTLS 1.2
SUITE = b"\x00\xa8"  # TLS_PSK_WITH_AES_128_GCM_SHA256
CT_CCS, CT_ALERT, CT_HANDSHAKE, CT_APPDATA = 20, 21, 22, 23
HT_CLIENT_HELLO, HT_SERVER_HELLO, HT_HELLO_VERIFY = 1, 2, 3
HT_SERVER_KEY_EXCHANGE, HT_SERVER_HELLO_DONE, HT_CLIENT_KEY_EXCHANGE, HT_FINISHED = 12, 14, 16, 20
RECORD_HEADER = 13  # type (1) + version (2) + epoch (2) + sequence number (6) + length (2)
HANDSHAKE_HEADER = 12  # type (1) + length (3) + message_seq (2) + fragment_offset (3) + fragment_length (3)
EXPLICIT_NONCE = 8
GCM_TAG = 16
VERIFY_DATA_LEN = 12
RETRANSMIT_S = 0.5  # first wait for an answer; doubles with every retransmission of the flight
FLIGHT_ATTEMPTS = 3  # transmissions of one flight before the handshake gives up
RECV_SIZE = 4096

ALERT_NAMES = {
    0: "close_notify", 10: "unexpected_message", 20: "bad_record_mac", 22: "record_overflow",
    40: "handshake_failure", 47: "illegal_parameter", 50: "decode_error", 51: "decrypt_error",
    70: "protocol_version", 71: "insufficient_security", 80: "internal_error", 115: "unknown_psk_identity",
}


class DTLSError(Exception):
    pass


class DTLSAlert(DTLSError):
    """The bridge sent an alert; `level` 1 = warning, 2 = fatal, `description` is the RFC 5246 code."""

    def __init__(self, level: int, description: int):
        self.level, self.description = level, description
        kind = {1: "warning", 2: "fatal"}.get(level, f"level {level}")
        name = ALERT_NAMES.get(description, "unknown")
        super().__init__(f"alert from bridge: {kind} {name} ({description})")


# ---- key schedule (RFC 5246 section 5, 8.1, 6.3; RFC 4279 section 2) -------
def _prf(secret: bytes, label: bytes, seed: bytes, size: int) -> bytes:
    """TLS 1.2 PRF with SHA-256: P_SHA256(secret, label + seed), cut to `size` bytes."""
    seed = label + seed
    out, a = b"", seed
    while len(out) < size:
        a = hmac.new(secret, a, hashlib.sha256).digest()
        out += hmac.new(secret, a + seed, hashlib.sha256).digest()
    return out[:size]


def _psk_premaster(psk: bytes) -> bytes:
    """Plain PSK premaster secret: uint16 N, N zero bytes, uint16 N, the PSK."""
    n = len(psk)
    return struct.pack(">H", n) + b"\x00" * n + struct.pack(">H", n) + psk


def _master_secret(psk: bytes, client_random: bytes, server_random: bytes) -> bytes:
    return _prf(_psk_premaster(psk), b"master secret", client_random + server_random, 48)


def _key_block(master: bytes, client_random: bytes, server_random: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    """AES-128-GCM has no MAC keys: client key, server key (16 each), client IV, server IV (4 each)."""
    kb = _prf(master, b"key expansion", server_random + client_random, 40)
    return kb[:16], kb[16:32], kb[32:36], kb[36:40]


def _verify_data(master: bytes, label: bytes, transcript: bytes) -> bytes:
    return _prf(master, label, hashlib.sha256(transcript).digest(), VERIFY_DATA_LEN)


# ---- parsing ---------------------------------------------------------------
def _parse_records(data: bytes) -> list[tuple[int, int, bytes, bytes]]:
    """Split a datagram into (type, epoch, seq_num (epoch + sequence, 8 bytes), fragment)."""
    out, pos = [], 0
    while pos < len(data):
        if len(data) - pos < RECORD_HEADER:
            raise DTLSError("truncated record header")
        ctype = data[pos]
        seqnum = data[pos + 3 : pos + 11]
        epoch = struct.unpack(">H", data[pos + 3 : pos + 5])[0]
        length = struct.unpack(">H", data[pos + 11 : pos + 13])[0]
        end = pos + RECORD_HEADER + length
        if end > len(data):
            raise DTLSError("record length exceeds datagram")
        out.append((ctype, epoch, seqnum, data[pos + RECORD_HEADER : end]))
        pos = end
    return out


def _parse_handshakes(payload: bytes) -> list[tuple[int, int, bytes, bytes]]:
    """Split a handshake record into (type, message_seq, full message, body); fragments are refused."""
    out, pos = [], 0
    while pos < len(payload):
        if len(payload) - pos < HANDSHAKE_HEADER:
            raise DTLSError("truncated handshake header")
        htype = payload[pos]
        length = int.from_bytes(payload[pos + 1 : pos + 4], "big")
        mseq = struct.unpack(">H", payload[pos + 4 : pos + 6])[0]
        frag_off = int.from_bytes(payload[pos + 6 : pos + 9], "big")
        frag_len = int.from_bytes(payload[pos + 9 : pos + 12], "big")
        if frag_off != 0 or frag_len != length:
            raise DTLSError(f"fragmented handshake message (type {htype}) is not supported")
        end = pos + HANDSHAKE_HEADER + length
        if end > len(payload):
            raise DTLSError("handshake length exceeds record")
        out.append((htype, mseq, payload[pos:end], payload[pos + HANDSHAKE_HEADER : end]))
        pos = end
    return out


def _parse_alert(fragment: bytes) -> DTLSAlert:
    if len(fragment) != 2:
        raise DTLSError("malformed alert from bridge")
    return DTLSAlert(fragment[0], fragment[1])


def _parse_hello_verify(body: bytes) -> bytes:
    """HelloVerifyRequest: server_version (2), cookie length (1), cookie."""
    if len(body) < 3 or len(body) != 3 + body[2]:
        raise DTLSError("malformed HelloVerifyRequest")
    return body[3:]


def _parse_server_hello(body: bytes) -> bytes:
    """Check version, suite and compression; return the server random."""
    if len(body) < 35:
        raise DTLSError("truncated ServerHello")
    sid_len = body[34]
    pos = 35 + sid_len
    if len(body) < pos + 3:
        raise DTLSError("truncated ServerHello")
    if body[:2] != VERSION:
        raise DTLSError(f"bridge answered with protocol version {body[:2].hex()}")
    if body[pos : pos + 2] != SUITE:
        raise DTLSError("bridge picked another cipher suite")
    if body[pos + 2] != 0:
        raise DTLSError("bridge picked a compression method")
    ext = body[pos + 3 :]
    if ext and (len(ext) < 2 or struct.unpack(">H", ext[:2])[0] != len(ext) - 2):
        raise DTLSError("malformed ServerHello extensions")
    return body[2:34]


class DTLSClient:
    def __init__(self, host: str, port: int, identity: str, psk: bytes, timeout: float = 5.0):
        self.addr = (host, port)
        self.identity = identity.encode()
        self.psk = psk
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.connect(self.addr)
        self.epoch = 0
        self.seq = {0: 0, 1: 0}
        self.msg_seq = 0
        self.write: tuple[bytes, bytes] | None = None
        self.connected = False

    # ---- records -------------------------------------------------------
    def _record(self, ctype: int, payload: bytes) -> bytes:
        seq = self.seq[self.epoch]
        self.seq[self.epoch] += 1
        seqbytes = struct.pack(">H", self.epoch) + seq.to_bytes(6, "big")
        if self.write is None:
            return bytes([ctype]) + VERSION + seqbytes + struct.pack(">H", len(payload)) + payload
        key, iv = self.write
        # RFC 5288: nonce = implicit IV (4) + explicit nonce (8, here the record's seq_num);
        # AAD = seq_num (8) + type + version + plaintext length.
        aad = seqbytes + bytes([ctype]) + VERSION + struct.pack(">H", len(payload))
        body = seqbytes + AESGCM(key).encrypt(iv + seqbytes, payload, aad)
        return bytes([ctype]) + VERSION + seqbytes + struct.pack(">H", len(body)) + body

    @staticmethod
    def _decrypt(key: bytes, iv: bytes, ctype: int, seqnum: bytes, fragment: bytes) -> bytes:
        if len(fragment) < EXPLICIT_NONCE + GCM_TAG:
            raise DTLSError("truncated encrypted record")
        explicit, sealed = fragment[:EXPLICIT_NONCE], fragment[EXPLICIT_NONCE:]
        aad = seqnum + bytes([ctype]) + VERSION + struct.pack(">H", len(sealed) - GCM_TAG)
        try:
            return AESGCM(key).decrypt(iv + explicit, sealed, aad)
        except InvalidTag:
            raise DTLSError("cannot decrypt the bridge's record (wrong client key?)") from None

    def _handshake(self, htype: int, body: bytes) -> bytes:
        msg = (bytes([htype]) + len(body).to_bytes(3, "big") + struct.pack(">H", self.msg_seq)
               + (0).to_bytes(3, "big") + len(body).to_bytes(3, "big") + body)
        self.msg_seq += 1
        return msg

    def _exchange(self, send_flight: Callable[[], None], on_record: Callable[[int, int, bytes, bytes], bool]) -> None:
        """Send a flight and feed the answers to `on_record` until it returns True.

        The flight is retransmitted after RETRANSMIT_S, then after twice that, and so on, up to
        FLIGHT_ATTEMPTS transmissions and never past `timeout`; the bridge often ignores the first copy.
        """
        deadline = time.monotonic() + self.timeout
        wait = RETRANSMIT_S
        for _ in range(FLIGHT_ATTEMPTS):
            send_flight()
            until = min(time.monotonic() + wait, deadline)
            while (remaining := until - time.monotonic()) > 0:
                self.sock.settimeout(remaining)
                try:
                    data = self.sock.recv(RECV_SIZE)
                except TimeoutError:
                    break
                for rec in _parse_records(data):
                    if on_record(*rec):
                        return
            if time.monotonic() >= deadline:
                break
            wait *= 2
        raise DTLSError("handshake timeout: no answer from the bridge")

    # ---- handshake -----------------------------------------------------
    def connect(self) -> None:
        # All 32 bytes random: gmt_unix_time need not be a real clock (RFC 5246 section 7.4.1.2).
        client_random = os.urandom(32)

        def client_hello(cookie: bytes) -> bytes:
            return (VERSION + client_random + b"\x00" + bytes([len(cookie)]) + cookie
                    + b"\x00\x02" + SUITE + b"\x01\x00")

        # Flights 1-4: ClientHello, optional HelloVerifyRequest + ClientHello with cookie, server flight.
        server: dict[int, tuple[int, bytes, bytes]] = {}  # message_seq -> (type, full message, body)
        state: dict = {"cookie": None, "done": None}
        ch = self._handshake(HT_CLIENT_HELLO, client_hello(b""))

        def on_hello(ctype: int, epoch: int, seqnum: bytes, frag: bytes) -> bool:
            if ctype == CT_ALERT and epoch == 0:
                raise _parse_alert(frag)
            if ctype != CT_HANDSHAKE or epoch != 0:
                return False
            for htype, mseq, msg, body in _parse_handshakes(frag):
                if htype == HT_HELLO_VERIFY:
                    # Only an answer to the cookie-less ClientHello counts. Later copies are replies
                    # to retransmitted first ClientHellos and are dropped (a rejected cookie ends in
                    # a handshake timeout, the caller starts over).
                    if state["cookie"] is None and not server:
                        state["cookie"] = _parse_hello_verify(body)
                        return True
                    continue
                if htype not in (HT_SERVER_HELLO, HT_SERVER_KEY_EXCHANGE, HT_SERVER_HELLO_DONE):
                    raise DTLSError(f"unexpected handshake message type {htype} from bridge")
                server.setdefault(mseq, (htype, msg, body))  # retransmitted copies count once
                if htype == HT_SERVER_HELLO_DONE:
                    state["done"] = mseq
            return state["done"] is not None and self._server_flight_complete(server, state["done"])

        self._exchange(lambda: self.sock.send(self._record(CT_HANDSHAKE, ch)), on_hello)
        if state["cookie"] is not None:
            ch = self._handshake(HT_CLIENT_HELLO, client_hello(state["cookie"]))
            self._exchange(lambda: self.sock.send(self._record(CT_HANDSHAKE, ch)), on_hello)

        first = min(m for m, (t, _, _) in server.items() if t == HT_SERVER_HELLO)
        flight = [server[m] for m in range(first, state["done"] + 1)]
        types = [t for t, _, _ in flight]
        if types not in ([HT_SERVER_HELLO, HT_SERVER_HELLO_DONE],
                         [HT_SERVER_HELLO, HT_SERVER_KEY_EXCHANGE, HT_SERVER_HELLO_DONE]):
            raise DTLSError(f"unexpected server flight {types}")
        server_random = _parse_server_hello(flight[0][2])
        if HT_SERVER_KEY_EXCHANGE in types:
            ske = flight[1][2]  # psk_identity_hint, ignored by Hue
            if len(ske) < 2 or len(ske) != 2 + struct.unpack(">H", ske[:2])[0]:
                raise DTLSError("malformed ServerKeyExchange")
        if flight[-1][2]:
            raise DTLSError("malformed ServerHelloDone")
        transcript = ch + b"".join(msg for _, msg, _ in flight)

        # Flight 5: ClientKeyExchange, ChangeCipherSpec, Finished (epoch 1).
        cke_msg = self._handshake(HT_CLIENT_KEY_EXCHANGE, struct.pack(">H", len(self.identity)) + self.identity)
        transcript += cke_msg
        master = _master_secret(self.psk, client_random, server_random)
        client_key, server_key, client_iv, server_iv = _key_block(master, client_random, server_random)
        fin_msg = self._handshake(HT_FINISHED, _verify_data(master, b"client finished", transcript))
        transcript += fin_msg
        expected = _verify_data(master, b"server finished", transcript)
        server_fin_seq = state["done"] + 1

        def send_final() -> None:
            # Each (re)transmission uses fresh record sequence numbers in both epochs.
            self.epoch, self.write = 0, None
            out = self._record(CT_HANDSHAKE, cke_msg) + self._record(CT_CCS, b"\x01")
            self.epoch, self.write = 1, (client_key, client_iv)
            self.sock.send(out + self._record(CT_HANDSHAKE, fin_msg))

        # Flight 6: the server's ChangeCipherSpec, then its Finished in epoch 1.
        ccs = {"seen": False}

        def on_final(ctype: int, epoch: int, seqnum: bytes, frag: bytes) -> bool:
            if epoch == 0:
                if ctype == CT_ALERT:
                    raise _parse_alert(frag)
                if ctype == CT_CCS:
                    if frag != b"\x01":
                        raise DTLSError("malformed ChangeCipherSpec")
                    ccs["seen"] = True
                return False  # epoch-0 handshake records here are retransmitted server flights
            if epoch != 1:
                return False
            if not ccs["seen"]:
                raise DTLSError("bridge sent an epoch 1 record before ChangeCipherSpec")
            plain = self._decrypt(server_key, server_iv, ctype, seqnum, frag)
            if ctype == CT_ALERT:
                raise _parse_alert(plain)
            if ctype != CT_HANDSHAKE:
                raise DTLSError(f"unexpected record type {ctype} before server Finished")
            msgs = _parse_handshakes(plain)
            if len(msgs) != 1 or msgs[0][0] != HT_FINISHED or msgs[0][1] != server_fin_seq:
                raise DTLSError("expected the server Finished")
            if not hmac.compare_digest(msgs[0][3], expected):
                raise DTLSError("server finished mismatch")
            return True

        self._exchange(send_final, on_final)
        self.sock.settimeout(None)
        self.connected = True

    @staticmethod
    def _server_flight_complete(server: dict, done: int) -> bool:
        """All messages from the ServerHello up to the ServerHelloDone have arrived."""
        hello = [m for m, (t, _, _) in server.items() if t == HT_SERVER_HELLO]
        return bool(hello) and all(m in server for m in range(min(hello), done + 1))

    def send(self, payload: bytes) -> None:
        if not self.connected:
            raise DTLSError("not connected")
        self.sock.send(self._record(CT_APPDATA, payload))

    def close(self) -> None:
        try:
            self.sock.send(self._record(CT_ALERT, b"\x01\x00"))  # close_notify
        except OSError:
            pass
        self.connected = False
        self.sock.close()
