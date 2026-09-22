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
    proxy.Proxy.upstreams = {proxy.UPSTREAM_HOST: SimpleNamespace(
        stats={"pool_dropped": 0, "pool_stale_timeout": 0})}
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

    proxy.Proxy.upstreams = {proxy.UPSTREAM_HOST: FakeUpstream()}
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


def _fixed_upstream(status: int, body: bytes, headers: dict[str, str]):
    """A stand-in Upstream that answers every request the same way and
    remembers what it was asked. Backed by a real HTTP server so the relay
    sees a genuine http.client response."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _reply

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class FakeUpstream:
        stats = {"pool_dropped": 0, "pool_stale_timeout": 0}
        calls: list[tuple[str, str]] = []

        def send(self, method, path, headers, body):
            self.calls.append((method, path))
            conn = http.client.HTTPConnection(*server.server_address, timeout=5)
            conn.request(method, path, body=body, headers=headers)
            return conn, conn.getresponse()

        def give_back(self, conn):
            conn.close()

    return FakeUpstream()


USAGE_PAYLOAD = {
    "five_hour": {"utilization": 10.0, "resets_at": "2026-09-22T05:50:00Z"},
    "seven_day": {"utilization": 72.0, "resets_at": "2026-09-22T15:00:00+00:00"},
    "limits": [
        {"kind": "session", "utilization": 10.0},
        {"kind": "weekly_all", "utilization": 72.0},
        {"kind": "weekly_scoped", "utilization": 30.0,
         "scope": {"model": {"display_name": "Fable"}}},
    ],
    "seven_day_breakdown": {"window_started_at": "2026-09-17T15:00:00Z",
                            "rows": [{"surface": "claude_code", "utilization": 79},
                                     {"surface": "cowork", "utilization": 15},
                                     {"surface": "chat", "utilization": 6}]},
    "nimbus_quill": None,
}


def _drain(q) -> list:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def test_routes_by_host_header(plain_server):
    api = _fixed_upstream(200, b"{}", {})
    site = _fixed_upstream(200, b"{}", {})
    proxy.Proxy.upstreams = {proxy.UPSTREAM_HOST: api, proxy.CLAUDE_AI_HOST: site}
    port = plain_server.server_address[1]

    for host, expected in ((f"127.0.0.1:{port}", api), ("claude.ai", site),
                           ("claude.ai:443", site), ("evil.example", api), (None, api)):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("GET", "/v1/models", skip_host=True)
        if host is not None:
            conn.putheader("Host", host)
        conn.endheaders()
        assert conn.getresponse().read() == b"{}"
        conn.close()
        assert expected.calls.pop() == ("GET", "/v1/models"), host
    assert not api.calls and not site.calls


def test_claude_usage_page_is_recorded_and_deduped(plain_server):
    site = _fixed_upstream(200, json.dumps(USAGE_PAYLOAD).encode(),
                           {"Content-Type": "application/json",
                            "anthropic-ratelimit-unified-7d-utilization": "0.99"})
    proxy.Proxy.upstreams = {proxy.UPSTREAM_HOST: object(), proxy.CLAUDE_AI_HOST: site}
    port = plain_server.server_address[1]

    def fetch(path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path, headers={"Host": "claude.ai"})
        body = conn.getresponse().read()
        conn.close()
        return body

    recorder = proxy.Proxy.recorder
    body = fetch("/api/organizations/org_1/usage?x=1")
    assert json.loads(body) == USAGE_PAYLOAD, "relayed body must be untouched"
    # The row is queued after the body is flushed to us, on the handler thread.
    assert _wait(lambda: not recorder.q.empty()), recorder.last_error
    items = _drain(recorder.q)
    assert [table for table, _ in items] == ["claude_usage"], \
        "claude.ai rate-limit headers must not land in the ratelimit table"
    row = items[0][1]
    assert row["org_id"] == "org_1"
    assert row["five_h_utilization"] == pytest.approx(0.10)
    assert row["seven_d_utilization"] == pytest.approx(0.72)
    from datetime import datetime, timezone
    assert row["five_h_reset"] == int(datetime(2026, 9, 22, 5, 50, tzinfo=timezone.utc).timestamp())
    assert row["seven_d_reset"] == int(datetime(2026, 9, 22, 15, tzinfo=timezone.utc).timestamp())
    assert json.loads(row["scoped"]) == {"Fable": pytest.approx(0.30)}
    assert json.loads(row["breakdown"]) == USAGE_PAYLOAD["seven_day_breakdown"]
    assert json.loads(row["raw"]) == USAGE_PAYLOAD

    fetch("/api/organizations/org_1/usage")
    assert not _wait(lambda: not recorder.q.empty(), timeout=0.5), \
        "unchanged meters are not re-recorded"

    fetch("/api/organizations/org_1/other")
    assert not _wait(lambda: not recorder.q.empty(), timeout=0.5), \
        "only the usage page is captured"
    assert recorder.last_error is None


def test_parse_claude_usage_rejects_unrelated_json():
    assert proxy.parse_claude_usage(b'{"hello": "world"}', "o") is None
    assert proxy.parse_claude_usage(b"[]", "o") is None
    assert proxy.parse_claude_usage(b"not json", "o") is None


def test_health_sums_pool_stats_across_upstreams(plain_server):
    proxy.Proxy.upstreams = {
        "a": SimpleNamespace(stats={"pool_dropped": 2, "pool_stale_timeout": 1}),
        "b": SimpleNamespace(stats={"pool_dropped": 3, "pool_stale_timeout": 0}),
    }
    port = plain_server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/__proxy/health")
    body = json.load(conn.getresponse())
    conn.close()
    assert body["pool_dropped"] == 5 and body["pool_stale_timeout"] == 1
    assert body["upstreams"] == ["a", "b"]


def test_leaf_is_reissued_when_a_name_is_added(tmp_path, monkeypatch):
    certs = tmp_path / "tls"
    monkeypatch.setattr(proxy, "CERT_DIR", certs)
    monkeypatch.setattr(proxy, "CA_KEY", certs / "ca.key")
    monkeypatch.setattr(proxy, "CA_CERT", certs / "ca.pem")
    monkeypatch.setattr(proxy, "LEAF_KEY", certs / "leaf.key")
    monkeypatch.setattr(proxy, "LEAF_CERT", certs / "leaf.pem")

    monkeypatch.setattr(proxy, "UPSTREAMS", (proxy.UPSTREAM_HOST,))
    done = proxy.ensure_certs()
    assert any("created CA" in line for line in done)
    assert proxy.leaf_names(proxy.LEAF_CERT) == {proxy.UPSTREAM_HOST, "localhost"}
    assert proxy.ensure_certs() == [], "a current leaf is left alone"

    monkeypatch.setattr(proxy, "UPSTREAMS", (proxy.UPSTREAM_HOST, "claude.ai"))
    done = proxy.ensure_certs()
    assert done == [f"issued leaf for {proxy.UPSTREAM_HOST}, claude.ai "
                    f"(valid {proxy.LEAF_DAYS} days)"]
    assert proxy.leaf_names(proxy.LEAF_CERT) >= {proxy.UPSTREAM_HOST, "claude.ai"}
    assert proxy.ensure_certs() == []


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
