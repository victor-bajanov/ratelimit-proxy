#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8"]
# ///
"""Tests for proxy.py.  Run with:  uv run test_proxy.py"""
from __future__ import annotations

import http.client
import json
import os
import resource
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import proxy


@pytest.fixture
def handler_env(tmp_path):
    """Class-level state serve() would normally install on Proxy."""
    proxy.Proxy.recorder = proxy.Recorder(tmp_path / "t.db")  # not started
    proxy.Proxy.upstream = SimpleNamespace(
        stats={"pool_dropped": 0, "pool_stale_timeout": 0})
    proxy.Proxy.started_at = time.monotonic()
    proxy.Proxy.counters = {"requests": 0, "errors": 0}
    proxy.Proxy.verbose = False


@pytest.fixture
def plain_server(handler_env):
    server = proxy.Server(("127.0.0.1", 0), proxy.Proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def tls_server(handler_env, tmp_path):
    key, cert = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run([proxy.OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
                    "-days", "1"], check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    server = proxy.TLSServer(("::1", 0), proxy.TLSProxy)
    server.tls = tls
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _client_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _wait(predicate, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_tls_handler_closes_its_socket_when_done(tls_server, monkeypatch):
    """wrap_socket detaches the fd from the socket socketserver closes, so the
    SSL socket must be closed by the handler itself -- not left to the GC,
    which may never get to it if a traceback keeps the handler alive."""
    retained = []
    original = proxy.TLSProxy.handle

    def handle(self):
        retained.append(self)  # stand-in for a lingering traceback reference
        original(self)

    monkeypatch.setattr(proxy.TLSProxy, "handle", handle)

    port = tls_server.server_address[1]
    conn = http.client.HTTPSConnection("::1", port, context=_client_ctx(), timeout=5)
    conn.request("GET", "/__proxy/health")
    assert conn.getresponse().status == 200
    conn.close()

    assert _wait(lambda: retained and retained[0].request.fileno() == -1), \
        "handler finished but its SSL socket is still open"


def test_health_reports_open_descriptors(plain_server):
    port = plain_server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/__proxy/health")
    body = json.load(conn.getresponse())
    conn.close()

    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert 3 <= body["fds"] <= soft
    assert body["fd_limit"] == soft


def _chunked_upstream(chunks: list[bytes], gap: float):
    """Plain HTTP server that dribbles `chunks` out `gap` seconds apart."""
    import socket

    def run(sock):
        conn, _ = sock.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n")
        for payload in chunks:
            conn.sendall(b"%X\r\n%s\r\n" % (len(payload), payload))
            time.sleep(gap)
        conn.sendall(b"0\r\n\r\n")
        conn.close()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    threading.Thread(target=run, args=(sock,), daemon=True).start()
    return sock.getsockname()[1]


def test_relay_forwards_each_chunk_as_it_arrives(plain_server):
    """A streamed response must reach the client chunk by chunk, not buffered
    until 64K or end of stream."""
    chunks = [f"data: {i}\n\n".encode() for i in range(4)]
    gap = 0.3
    upstream_port = _chunked_upstream(chunks, gap)

    class FakeUpstream:
        stats = {"pool_dropped": 0, "pool_stale_timeout": 0}

        def send(self, method, path, headers, body):
            conn = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=5)
            conn.request(method, path, body=body, headers=headers)
            return conn, conn.getresponse()

        def give_back(self, conn):
            conn.close()

    proxy.Proxy.upstream = FakeUpstream()
    port = plain_server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/v1/messages", body=b"{}")
    resp = conn.getresponse()
    t0 = time.monotonic()
    first = resp.read1(65536)
    first_at = time.monotonic() - t0
    rest = b""
    while chunk := resp.read1(65536):
        rest += chunk
    conn.close()

    assert first == chunks[0], first
    assert first_at < gap, f"first chunk held for {first_at:.2f}s"
    assert first + rest == b"".join(chunks)


def test_raise_fd_limit_lifts_soft_limit():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        assert proxy.raise_fd_limit() >= 1024
        assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] >= 1024
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_open_fds_counts_a_new_descriptor(tmp_path):
    before = proxy.open_fds()
    with (tmp_path / "f").open("w"):
        assert proxy.open_fds() == before + 1
    assert proxy.open_fds() == before


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", *sys.argv[1:]]))
