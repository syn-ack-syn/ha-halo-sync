"""Minimal DTLS 1.2 client for TLS_PSK_WITH_AES_128_GCM_SHA256 (Hue Entertainment).

Pure Python on top of `cryptography`; implements only what the Hue bridge needs:
cookie exchange, PSK key exchange, AES-128-GCM records, application data out.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import struct
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = b"\xfe\xfd"  # DTLS 1.2
SUITE = b"\x00\xa8"  # TLS_PSK_WITH_AES_128_GCM_SHA256
CT_CCS, CT_ALERT, CT_HANDSHAKE, CT_APPDATA = 20, 21, 22, 23
HT_CLIENT_HELLO, HT_SERVER_HELLO, HT_HELLO_VERIFY = 1, 2, 3
HT_SERVER_KEY_EXCHANGE, HT_SERVER_HELLO_DONE, HT_CLIENT_KEY_EXCHANGE, HT_FINISHED = 12, 14, 16, 20
RETRANSMIT_S = 0.5


class DTLSError(Exception):
    pass


def _prf(secret: bytes, label: bytes, seed: bytes, size: int) -> bytes:
    seed = label + seed
    out, a = b"", seed
    while len(out) < size:
        a = hmac.new(secret, a, hashlib.sha256).digest()
        out += hmac.new(secret, a + seed, hashlib.sha256).digest()
    return out[:size]


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
        self.transcript = b""
        self.server_seen: set[int] = set()
        self.write = None

    # ---- records -------------------------------------------------------
    def _record(self, ctype: int, payload: bytes) -> bytes:
        seq = self.seq[self.epoch]
        self.seq[self.epoch] += 1
        seqbytes = struct.pack(">H", self.epoch) + seq.to_bytes(6, "big")
        if self.write is None:
            return bytes([ctype]) + VERSION + seqbytes + struct.pack(">H", len(payload)) + payload
        key, iv = self.write
        aad = seqbytes + bytes([ctype]) + VERSION + struct.pack(">H", len(payload))
        body = seqbytes + AESGCM(key).encrypt(iv + seqbytes, payload, aad)
        return bytes([ctype]) + VERSION + seqbytes + struct.pack(">H", len(body)) + body

    def _handshake(self, htype: int, body: bytes, hashed: bool = True) -> bytes:
        hdr = bytes([htype]) + len(body).to_bytes(3, "big") + struct.pack(">H", self.msg_seq)
        msg = hdr + (0).to_bytes(3, "big") + len(body).to_bytes(3, "big") + body
        self.msg_seq += 1
        if hashed:
            self.transcript += msg
        return msg

    def _recv_handshakes(self, until: set[int], resend) -> dict[int, bytes]:
        """Read datagrams until one of `until` handshake types arrives; returns type -> body.

        `resend()` retransmits our last flight; the bridge often ignores the first copy.
        """
        got: dict[int, bytes] = {}
        deadline = time.time() + self.timeout
        while not (until & got.keys()):
            remaining = deadline - time.time()
            if remaining <= 0:
                raise DTLSError(f"timeout waiting for {until}")
            self.sock.settimeout(min(RETRANSMIT_S, remaining))
            try:
                data = self.sock.recv(4096)
            except TimeoutError:
                resend()
                continue
            while data:
                ctype, length = data[0], struct.unpack(">H", data[11:13])[0]
                payload, data = data[13 : 13 + length], data[13 + length :]
                if ctype == CT_ALERT:
                    raise DTLSError(f"alert from bridge: {payload.hex()}")
                if ctype != CT_HANDSHAKE:
                    continue
                while payload:
                    htype, hlen = payload[0], int.from_bytes(payload[1:4], "big")
                    mseq = struct.unpack(">H", payload[4:6])[0]
                    msg, payload = payload[: 12 + hlen], payload[12 + hlen :]
                    if htype == HT_HELLO_VERIFY:
                        got[htype] = msg[12:]
                    elif mseq not in self.server_seen:  # retransmitted copies must not enter the transcript twice
                        self.server_seen.add(mseq)
                        got[htype] = msg[12:]
                        self.transcript += msg
        return got

    # ---- handshake -----------------------------------------------------
    def connect(self) -> None:
        client_random = struct.pack(">I", int(time.time())) + os.urandom(28)

        def client_hello(cookie: bytes) -> bytes:
            return (VERSION + client_random + b"\x00" + bytes([len(cookie)]) + cookie
                    + b"\x00\x02" + SUITE + b"\x01\x00")

        ch1 = self._handshake(HT_CLIENT_HELLO, client_hello(b""), hashed=False)
        send_ch1 = lambda: self.sock.send(self._record(CT_HANDSHAKE, ch1))  # noqa: E731
        send_ch1()
        got = self._recv_handshakes({HT_HELLO_VERIFY, HT_SERVER_HELLO_DONE}, send_ch1)
        if HT_HELLO_VERIFY in got:
            hv = got[HT_HELLO_VERIFY]
            cookie = hv[3 : 3 + hv[2]]
            self.transcript = b""
            ch2 = self._handshake(HT_CLIENT_HELLO, client_hello(cookie))
            send_ch2 = lambda: self.sock.send(self._record(CT_HANDSHAKE, ch2))  # noqa: E731
            send_ch2()
            got = self._recv_handshakes({HT_SERVER_HELLO_DONE}, send_ch2)
        sh = got[HT_SERVER_HELLO]
        if sh[35 + sh[34] : 37 + sh[34]] != SUITE:
            raise DTLSError("bridge picked another cipher suite")
        server_random = sh[2:34]

        cke = struct.pack(">H", len(self.identity)) + self.identity
        cke_msg = self._handshake(HT_CLIENT_KEY_EXCHANGE, cke)
        n = len(self.psk)
        pms = struct.pack(">H", n) + b"\x00" * n + struct.pack(">H", n) + self.psk
        master = _prf(pms, b"master secret", client_random + server_random, 48)
        kb = _prf(master, b"key expansion", server_random + client_random, 40)
        client_key, server_key, client_iv, server_iv = kb[:16], kb[16:32], kb[32:36], kb[36:40]

        verify = _prf(master, b"client finished", hashlib.sha256(self.transcript).digest(), 12)
        fin_msg = self._handshake(HT_FINISHED, verify)

        def send_final() -> None:
            # Each (re)transmission uses fresh record sequence numbers in both epochs.
            self.epoch, self.write = 0, None
            flight = self._record(CT_HANDSHAKE, cke_msg) + self._record(CT_CCS, b"\x01")
            self.epoch, self.write = 1, (client_key, client_iv)
            self.sock.send(flight + self._record(CT_HANDSHAKE, fin_msg))

        send_final()

        # Wait for the server's ChangeCipherSpec + Finished (encrypted, not verified further).
        deadline = time.time() + self.timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise DTLSError("timeout waiting for server Finished")
            self.sock.settimeout(min(RETRANSMIT_S, remaining))
            try:
                data = self.sock.recv(4096)
            except TimeoutError:
                send_final()
                continue
            if data and data[0] == CT_ALERT:
                raise DTLSError(f"alert from bridge: {data[13:].hex()}")
            # Server Finished arrives in epoch 1 as an encrypted handshake record.
            pos = 0
            while pos < len(data):
                ctype, epoch = data[pos], struct.unpack(">H", data[pos + 3 : pos + 5])[0]
                length = struct.unpack(">H", data[pos + 11 : pos + 13])[0]
                if ctype == CT_HANDSHAKE and epoch == 1:
                    self.sock.settimeout(None)
                    return
                pos += 13 + length

    def send(self, payload: bytes) -> None:
        self.sock.send(self._record(CT_APPDATA, payload))

    def close(self) -> None:
        try:
            self.sock.send(self._record(CT_ALERT, b"\x01\x00"))  # close_notify
        except OSError:
            pass
        self.sock.close()
