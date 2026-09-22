"""Mesh protocol checks using in-memory connections and temporary identities."""

import ctypes
import json
import struct
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from latticeshadow import homomorphic, p2p
from latticeshadow.trust import DeviceTrustStore


def frame(payload):
    body = json.dumps(payload).encode("utf-8")
    return struct.pack("!I", len(body)) + body


class MemorySocket:
    def __init__(self, incoming=b""):
        self.incoming = incoming
        self.sent = b""
        self.closed = False
        self.timeout = None
        self.address = None

    def recv(self, length):
        chunk = self.incoming[: min(length, 2)]
        self.incoming = self.incoming[len(chunk):]
        return chunk

    def sendall(self, data):
        self.sent += data

    def settimeout(self, timeout):
        self.timeout = timeout

    def gettimeout(self):
        return self.timeout

    def connect(self, address):
        self.address = address

    def close(self):
        self.closed = True


def paired_stores(tmp_path):
    local = DeviceTrustStore(str(tmp_path / "local"))
    peer = DeviceTrustStore(str(tmp_path / "peer"))
    local.trust_device("peer", peer.public_key_hex())
    peer.trust_device("local", local.public_key_hex())
    return local, peer


def test_native_wrapper_rejects_short_vectors_before_call(monkeypatch):
    fake_lib = SimpleNamespace(
        homomorphic_xor=Mock(side_effect=AssertionError("native called")),
        decrypt_distance=Mock(side_effect=AssertionError("native called")),
    )
    monkeypatch.setattr(homomorphic, "lib", fake_lib)

    with pytest.raises(ValueError, match="enc_a must have shape"):
        homomorphic.evaluate_distance_homomorphically(
            np.zeros((2, 1), dtype=np.uint32),
            np.zeros(2, dtype=np.uint32),
            np.zeros(2, dtype=np.uint32),
        )
    with pytest.raises(ValueError, match="enc_a_sum must have shape"):
        homomorphic.decrypt_distance(
            np.zeros(homomorphic.LWE_N, dtype=np.uint32),
            np.zeros(1, dtype=np.uint32),
            0,
        )
    fake_lib.homomorphic_xor.assert_not_called()
    fake_lib.decrypt_distance.assert_not_called()


def test_native_wrapper_copies_strided_rows(monkeypatch):
    calls = []

    def homomorphic_xor(a_in, _b_in, _public_bit, _a_out, b_out, length):
        calls.append(np.ctypeslib.as_array(a_in, shape=(length,)).copy())
        ctypes.cast(b_out, ctypes.POINTER(ctypes.c_uint32))[0] = 0

    monkeypatch.setattr(homomorphic, "lib", SimpleNamespace(homomorphic_xor=homomorphic_xor))
    enc_a = np.zeros((2, homomorphic.LWE_N * 2), dtype=np.uint32)[:, ::2]
    enc_a[0] = np.arange(homomorphic.LWE_N, dtype=np.uint32)
    enc_a[1] = np.arange(homomorphic.LWE_N, dtype=np.uint32) + 1

    homomorphic.evaluate_distance_homomorphically(
        enc_a, np.zeros(2, dtype=np.uint32), np.zeros(2, dtype=np.uint32)
    )

    assert len(calls) == 2
    np.testing.assert_array_equal(calls[0], enc_a[0])
    np.testing.assert_array_equal(calls[1], enc_a[1])


def test_frame_reader_handles_split_header_and_rejects_oversize():
    assert p2p._read_frame(MemorySocket(frame({"type": "QUERY"}))) == b'{"type": "QUERY"}'
    with pytest.raises(ValueError, match="size limit"):
        p2p._read_frame(MemorySocket(struct.pack("!I", p2p.MAX_FRAME_BYTES + 1)))
    with pytest.raises(EOFError, match="Incomplete"):
        p2p._read_frame(MemorySocket(struct.pack("!I", 5) + b"ab"))


def test_frame_reader_enforces_total_deadline(monkeypatch):
    clock = iter([0.0, 1.0, 2.0, 3.1])
    monkeypatch.setattr(p2p.time, "monotonic", lambda: next(clock))
    conn = MemorySocket(frame({"type": "QUERY"}))
    conn.settimeout(3.0)
    with pytest.raises(TimeoutError, match="timed out"):
        p2p._read_frame(conn)
    assert conn.timeout == 3.0


def test_incoming_query_requires_trusted_signature(tmp_path):
    local_store, peer_store = paired_stores(tmp_path)
    node = object.__new__(p2p.LocalMeshNode)
    node.node_id = "local"
    node.trust_store = local_store
    node._handle_incoming_query = Mock(return_value=[])

    unsigned = MemorySocket(frame({"type": "QUERY", "node_id": "peer"}))
    node._handle_tcp_connection(unsigned)
    node._handle_incoming_query.assert_not_called()
    assert unsigned.sent == b""
    assert unsigned.timeout == p2p.SOCKET_TIMEOUT
    assert unsigned.closed

    signed = MemorySocket(frame(peer_store.sign_payload({"type": "QUERY", "node_id": "peer"})))
    node._handle_tcp_connection(signed)
    node._handle_incoming_query.assert_called_once()
    response = json.loads(signed.sent[4:].decode("utf-8"))
    assert response["type"] == "QUERY_RESULT"
    assert response["node_id"] == "local"
    assert peer_store.verify_payload(response)[0]
    assert signed.closed


def test_incoming_query_rejects_short_ciphertext_before_vault_read():
    node = object.__new__(p2p.LocalMeshNode)
    connect = Mock(side_effect=AssertionError("vault queried"))
    node.vault = SimpleNamespace(_store=SimpleNamespace(_connect=connect), name="test")
    assert node._handle_incoming_query({"enc_a": [[1]], "enc_b": [1]}) == []
    connect.assert_not_called()


def test_outgoing_query_uses_trust_and_validates_response(tmp_path, monkeypatch):
    local_store, peer_store = paired_stores(tmp_path)
    node = object.__new__(p2p.LocalMeshNode)
    node.node_id = "local"
    node.trust_store = local_store
    node.vault = SimpleNamespace(_privacy=None)
    node.peers = {
        "peer": ("192.0.2.1", 5001, time.time()),
        "rogue": ("192.0.2.2", 5002, time.time()),
    }
    monkeypatch.setattr(
        homomorphic,
        "encrypt_bitmask",
        lambda _key, _mask: (
            np.zeros((128, homomorphic.LWE_N), dtype=np.uint32),
            np.zeros(128, dtype=np.uint32),
        ),
    )
    decrypt = Mock(return_value=1)
    monkeypatch.setattr(homomorphic, "decrypt_distance", decrypt)

    valid_sum = np.zeros(homomorphic.LWE_N, dtype=np.uint32)
    responses = [
        {"node_id": "peer", "doc_id": "short", "document": "bad", "a_sum": [0], "b_sum": 0},
        {
            "node_id": "peer",
            "doc_id": "valid",
            "document": "ciphertext",
            "a_sum": valid_sum.tolist(),
            "b_sum": 0,
            "proof": p2p.generate_distance_snark_proof(valid_sum, 0, 35),
        },
    ]
    outgoing = MemorySocket(
        frame(peer_store.sign_payload({"type": "QUERY_RESULT", "node_id": "peer", "responses": responses}))
    )
    sockets = Mock(return_value=outgoing)
    monkeypatch.setattr(p2p.socket, "socket", sockets)

    matches = node.broadcast_search(np.zeros(128, dtype=np.uint32), np.zeros(homomorphic.LWE_N, dtype=np.uint32))

    assert matches == [
        {"node_id": "peer", "doc_id": "valid", "document": "ciphertext", "distance": 1, "proof_valid": True}
    ]
    decrypt.assert_called_once()
    assert outgoing.address == ("192.0.2.1", 5001)
    assert outgoing.closed
    assert sockets.call_count == 1
    assert peer_store.verify_payload(json.loads(outgoing.sent[4:].decode("utf-8")))[0]


def test_early_response_rejection_closes_socket(tmp_path, monkeypatch):
    local_store, _ = paired_stores(tmp_path)
    node = object.__new__(p2p.LocalMeshNode)
    node.node_id = "local"
    node.trust_store = local_store
    node.peers = {"peer": ("192.0.2.1", 5001, time.time())}
    monkeypatch.setattr(
        homomorphic,
        "encrypt_bitmask",
        lambda _key, _mask: (np.zeros((128, homomorphic.LWE_N), dtype=np.uint32), np.zeros(128, dtype=np.uint32)),
    )
    outgoing = MemorySocket(frame({"type": "QUERY_RESULT", "node_id": "peer", "responses": []}))
    monkeypatch.setattr(p2p.socket, "socket", Mock(return_value=outgoing))

    assert node.broadcast_search(np.zeros(128, dtype=np.uint32), np.zeros(homomorphic.LWE_N, dtype=np.uint32)) == []
    assert outgoing.closed


def test_tcp_accept_loop_caps_pending_connections(monkeypatch):
    node = object.__new__(p2p.LocalMeshNode)
    node.running = True
    node._connection_slots = threading.BoundedSemaphore(1)
    first, second = MemorySocket(), MemorySocket()

    class Server:
        def __init__(self):
            self.calls = 0

        def accept(self):
            self.calls += 1
            if self.calls == 2:
                node.running = False
            return (first if self.calls == 1 else second), ("192.0.2.1", 1)

    started = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            self.target = target
            self.args = args

        def start(self):
            started.append(self)

    node.tcp_server = Server()
    monkeypatch.setattr(p2p.threading, "Thread", FakeThread)
    node._tcp_accept_loop()

    assert len(started) == 1
    assert second.closed
    started[0].target(*started[0].args)
    assert first.closed
    assert first.timeout == p2p.SOCKET_TIMEOUT
    assert node._connection_slots.acquire(blocking=False)
