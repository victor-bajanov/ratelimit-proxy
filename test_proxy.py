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
    proxy.Proxy.config = proxy.Config(tmp_path / "config.json")  # absent: all off
    proxy.Proxy.upstream = SimpleNamespace(
        stats={"pool_dropped": 0, "pool_stale_timeout": 0})
    proxy.Proxy.started_at = time.monotonic()
    proxy.Proxy.counters = {"requests": 0, "errors": 0}
    proxy.Proxy.verbose = False


@pytest.fixture
def plain_server(handler_env):
    server = proxy.Server(("127.0.0.1", 0), proxy.Proxy)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
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
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
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


USAGE_PAYLOAD = {
    "five_hour": {"utilization": 10.0, "resets_at": "2026-09-22T05:50:00Z"},
    "seven_day": {"utilization": 72.0, "resets_at": "2026-09-22T15:00:00+00:00"},
    "limits": [
        {"kind": "session", "utilization": 10.0},
        {"kind": "weekly_all", "utilization": 72.0},
        {"kind": "weekly_scoped", "utilization": 30.0,
         "scope": {"model": {"display_name": "Fable"}}},
    ],
    "seven_day_breakdown": {"as_of": "2026-09-23T05:09:18.238571+00:00", "window_started_at": "2026-09-22T15:00:00.216686+00:00", "rows": [{"key": "claude_code", "display_name": "Claude Code", "percent": 79}, {"key": "chat", "display_name": "Chats", "percent": 2}, {"key": "cowork", "display_name": "Cowork", "percent": 19}, {"key": "other", "display_name": "Other", "percent": 0}]},
    "nimbus_quill": None,
}


def test_parse_claude_usage_flattens_the_usage_page():
    from datetime import datetime, timezone
    row = proxy.parse_claude_usage(json.dumps(USAGE_PAYLOAD).encode(), "org_1")
    assert row["org_id"] == "org_1"
    assert row["five_h_utilization"] == pytest.approx(0.10)
    assert row["seven_d_utilization"] == pytest.approx(0.72)
    assert row["five_h_reset"] == int(datetime(2026, 9, 22, 5, 50, tzinfo=timezone.utc).timestamp())
    assert row["seven_d_reset"] == int(datetime(2026, 9, 22, 15, tzinfo=timezone.utc).timestamp())
    assert json.loads(row["scoped"]) == {"Fable": pytest.approx(0.30)}
    assert json.loads(row["breakdown"]) == USAGE_PAYLOAD["seven_day_breakdown"]
    assert json.loads(row["raw"]) == USAGE_PAYLOAD


def test_parse_claude_usage_reads_scoped_meters_from_percent():
    """limits[] as the page sends it since at least 23 Sep: `percent`, not
    `utilization`, and a scope that also names a surface."""
    payload = dict(USAGE_PAYLOAD, limits=[
        {"group": "session", "kind": "session", "percent": 13, "scope": None},
        {"group": "weekly", "kind": "weekly_all", "percent": 37, "scope": None},
        {"group": "weekly", "kind": "weekly_scoped", "percent": 20,
         "resets_at": "2026-10-20T15:00:00.041117+00:00", "is_active": False,
         "scope": {"model": {"display_name": "Fable", "id": None}, "surface": None},
         "severity": "normal"},
    ])
    row = proxy.parse_claude_usage(json.dumps(payload).encode(), "org_1")
    assert json.loads(row["scoped"]) == {"Fable": pytest.approx(0.20)}


def test_parse_claude_usage_rejects_unrelated_json():
    assert proxy.parse_claude_usage(b'{"hello": "world"}', "o") is None
    assert proxy.parse_claude_usage(b"[]", "o") is None
    assert proxy.parse_claude_usage(b"not json", "o") is None


def _settle(recorder, timeout=5.0):
    """Block until the recorder has written (or dropped) everything queued."""
    done = threading.Thread(target=recorder.q.join, daemon=True)
    done.start()
    done.join(timeout)
    assert not done.is_alive(), "recorder never drained its queue"


def _usage_rows(db: Path) -> list:
    with proxy.connect(db) as conn:
        return conn.execute("SELECT * FROM claude_usage ORDER BY id").fetchall()


def _with_breakdown(**changes) -> dict:
    """USAGE_PAYLOAD with its seven_day_breakdown fields replaced."""
    return dict(USAGE_PAYLOAD, seven_day_breakdown=dict(
        USAGE_PAYLOAD["seven_day_breakdown"], **changes))


def test_recorder_writes_claude_usage_rows_only_on_change(tmp_path):
    recorder = proxy.Recorder(tmp_path / "t.db")
    recorder.start()

    def parsed(payload) -> dict:
        return proxy.parse_claude_usage(json.dumps(payload).encode(), "org_1")

    row = parsed(USAGE_PAYLOAD)
    # as_of is stamped per response, so on its own it is not a change.
    restamped = parsed(_with_breakdown(as_of="2026-09-23T05:09:18.576814+00:00"))
    shifted = parsed(_with_breakdown(rows=[
        {"key": "claude_code", "display_name": "Claude Code", "percent": 80}]))
    recorder.claude_usage(dict(row, ts=1000))
    recorder.claude_usage(dict(row, ts=1060))  # same meters, a minute later
    recorder.claude_usage(dict(restamped, ts=1090))
    recorder.claude_usage(dict(row, ts=1120, five_h_utilization=0.11))
    recorder.claude_usage(dict(row, ts=1180, five_h_utilization=0.11, org_id="org_2"))
    recorder.claude_usage(dict(shifted, ts=1240, five_h_utilization=0.11, org_id="org_2"))
    _settle(recorder)

    rows = _usage_rows(tmp_path / "t.db")
    assert [(r["ts"], r["org_id"], r["five_h_utilization"]) for r in rows] == [
        (1000, "org_1", pytest.approx(0.10)),
        (1120, "org_1", pytest.approx(0.11)),
        (1180, "org_2", pytest.approx(0.11)),
        (1240, "org_2", pytest.approx(0.11)),
    ]
    assert json.loads(rows[0]["raw"]) == USAGE_PAYLOAD
    assert recorder.last_error is None


ORG = "a9b7329c-ea04-44d2-8e73-92334358e425"
# A minute ago, as the extension's new Date().toISOString() would put it.
OBSERVED_TS = int(time.time()) - 60
OBSERVED = time.strftime("%Y-%m-%dT%H:%M:%S.456Z", time.gmtime(OBSERVED_TS))
EXTENSION = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


def _envelope(**overrides) -> bytes:
    return json.dumps({"org_id": ORG, "observed_at": OBSERVED,
                       "payload": USAGE_PAYLOAD, **overrides}).encode()


@pytest.fixture
def no_upstream(handler_env):
    """An upstream that must never be reached: ingest is answered locally."""
    class Untouchable:
        stats = {"pool_dropped": 0, "pool_stale_timeout": 0}
        calls: list = []

        def send(self, method, path, headers, body):
            self.calls.append((method, path))
            raise AssertionError(f"relayed upstream: {method} {path}")

    proxy.Proxy.upstream = Untouchable()
    yield
    assert Untouchable.calls == [], "ingest traffic reached the upstream"


def _ingest(port, body: bytes | None = None, *, method="POST", headers=None,
            tls=False):
    """POST to the ingest route like the extension does; returns the response
    with its body already read."""
    sent = {"Origin": EXTENSION, "Content-Type": "application/json"}
    sent.update(headers or {})
    sent = {k: v for k, v in sent.items() if v is not None}
    if tls:
        conn = http.client.HTTPSConnection("::1", port, context=_client_ctx(), timeout=5)
    else:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, "/__proxy/claude-usage",
                 body=_envelope() if body is None and method == "POST" else body,
                 headers=sent)
    response = conn.getresponse()
    response.body = response.read()
    conn.close()
    return response


def test_ingest_records_the_usage_page_and_dedupes(plain_server, no_upstream):
    recorder = proxy.Proxy.recorder
    recorder.start()
    port = plain_server.server_address[1]

    first = _ingest(port)
    assert first.status == 204, first.body
    assert _ingest(port).status == 204, "a duplicate is still a success"
    _settle(recorder)

    rows = _usage_rows(recorder.db_path)
    assert len(rows) == 1, "identical meters are recorded once"
    assert rows[0]["org_id"] == ORG
    assert rows[0]["ts"] == OBSERVED_TS
    assert json.loads(rows[0]["raw"]) == USAGE_PAYLOAD
    assert recorder.last_error is None
    assert proxy.Proxy.counters["requests"] == 0, "not a proxied request"


def test_ingest_dedupes_page_loads_that_differ_only_in_as_of(plain_server, no_upstream):
    """claude.ai stamps as_of per response, 0.3 s apart in the spike's captures,
    so a page that fetches /usage three times per load must still be one row;
    the row keeps the first capture's full breakdown."""
    recorder = proxy.Proxy.recorder
    recorder.start()
    port = plain_server.server_address[1]
    for as_of in ("2026-09-23T05:09:18.238571+00:00", "2026-09-23T05:09:18.576814+00:00"):
        response = _ingest(port, _envelope(payload=_with_breakdown(as_of=as_of)))
        assert response.status == 204, response.body
    _settle(recorder)

    rows = _usage_rows(recorder.db_path)
    assert len(rows) == 1
    assert json.loads(rows[0]["breakdown"])["as_of"] == "2026-09-23T05:09:18.238571+00:00"
    assert json.loads(rows[0]["raw"])["seven_day_breakdown"]["as_of"] \
        == "2026-09-23T05:09:18.238571+00:00"


def test_ingest_dedupes_page_loads_with_a_jittering_window_start(plain_server,
                                                                 no_upstream):
    """Seen live on 23 Sep: two loads 37 s apart, meters and rows identical,
    window_started_at differing below the second. Still one row, stored as
    first captured."""
    recorder = proxy.Proxy.recorder
    recorder.start()
    port = plain_server.server_address[1]
    for as_of, started in (
            ("2026-09-23T05:40:01.100000+00:00", "2026-09-22T15:00:00.952333+00:00"),
            ("2026-09-23T05:40:38.200000+00:00", "2026-09-22T15:00:00.903325+00:00")):
        payload = _with_breakdown(as_of=as_of, window_started_at=started)
        assert _ingest(port, _envelope(payload=payload)).status == 204
    _settle(recorder)

    rows = _usage_rows(recorder.db_path)
    assert len(rows) == 1
    assert json.loads(rows[0]["breakdown"])["window_started_at"] \
        == "2026-09-22T15:00:00.952333+00:00"
    assert json.loads(rows[0]["raw"])["seven_day_breakdown"]["window_started_at"] \
        == "2026-09-22T15:00:00.952333+00:00"


@pytest.mark.parametrize("first, second, rows", [
    # Jitter that straddles the hour: truncation would split these, rounding
    # to the nearest minute does not.
    ("2026-09-22T15:00:00.952333+00:00", "2026-09-22T14:59:59.990000+00:00", 1),
    # The next weekly window is a real change.
    ("2026-09-22T15:00:00.952333+00:00", "2026-09-29T15:00:00.903325+00:00", 2),
    # So is anything a minute or more away, should the window ever move a little.
    ("2026-09-22T15:00:00.952333+00:00", "2026-09-22T15:01:00.952333+00:00", 2),
    # A window start that is not a time is compared as it is.
    ("soon", "later", 2),
])
def test_recorder_dedupes_window_start_to_the_minute(tmp_path, first, second, rows):
    recorder = proxy.Recorder(tmp_path / "t.db")
    recorder.start()
    for ts, started in ((1000, first), (1060, second)):
        row = proxy.parse_claude_usage(
            json.dumps(_with_breakdown(window_started_at=started)).encode(), "org_1")
        recorder.claude_usage(dict(row, ts=ts))
    _settle(recorder)
    assert len(_usage_rows(tmp_path / "t.db")) == rows


def test_ingest_only_accepts_post(plain_server, no_upstream):
    port = plain_server.server_address[1]
    for method in ("GET", "HEAD", "PUT", "DELETE", "PATCH"):
        response = _ingest(port, method=method,
                           body=_envelope() if method in ("PUT", "PATCH") else None)
        assert response.status == 405, method
        assert response.getheader("Allow") == "POST", method
    assert proxy.Proxy.recorder.q.empty()


@pytest.mark.parametrize("origin", [
    None, "http://127.0.0.1:8787", "https://claude.ai", "chrome-extension://",
])
def test_ingest_refuses_anything_but_an_extension_origin(plain_server, no_upstream,
                                                         origin):
    response = _ingest(plain_server.server_address[1], headers={"Origin": origin})
    assert response.status == 403
    assert proxy.Proxy.recorder.q.empty()


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/jsonx"])
def test_ingest_refuses_bodies_a_page_could_send_without_preflight(
        plain_server, no_upstream, content_type):
    response = _ingest(plain_server.server_address[1],
                       headers={"Content-Type": content_type})
    assert response.status == 415
    assert proxy.Proxy.recorder.q.empty()


def test_ingest_tolerates_a_charset_on_json(plain_server, no_upstream):
    response = _ingest(plain_server.server_address[1],
                       headers={"Content-Type": "Application/JSON; charset=utf-8"})
    assert response.status == 204
    assert not proxy.Proxy.recorder.q.empty()


def _padded_envelope(size: int) -> bytes:
    """A valid envelope padded to exactly `size` bytes."""
    empty = _envelope(pad="")
    body = _envelope(pad="x" * (size - len(empty)))
    assert len(body) == size
    return body


def test_ingest_takes_bodies_up_to_256k(plain_server, no_upstream):
    assert _ingest(plain_server.server_address[1],
                   _padded_envelope(proxy.INGEST_CAP)).status == 204


def _raw_ingest(port, extra_headers: bytes) -> bytes:
    """Send only the head of an ingest POST and return everything the server
    says back. No body follows: each of these must be refused on the headers
    alone, and body bytes left unread would let the kernel answer the close
    with a reset that eats the reply."""
    import socket
    head = (b"POST /__proxy/claude-usage HTTP/1.1\r\nHost: x\r\n"
            b"Origin: " + EXTENSION.encode() + b"\r\n"
            b"Content-Type: application/json\r\n" + extra_headers + b"\r\n")
    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
        sock.sendall(head)
        reply = b""
        while chunk := sock.recv(65536):
            reply += chunk
    return reply


def test_ingest_refuses_an_oversize_body_without_reading_it(plain_server, no_upstream):
    """The claim alone is enough: were the server to wait for the body it
    announced, this would time out instead."""
    reply = _raw_ingest(plain_server.server_address[1],
                        b"Content-Length: %d\r\n" % (proxy.INGEST_CAP + 1))
    assert reply.startswith(b"HTTP/1.1 413 "), reply[:80]
    assert proxy.Proxy.recorder.q.empty()


def test_ingest_refuses_a_content_length_too_long_to_convert(plain_server, no_upstream):
    """Past 4300 digits int() raises rather than converting."""
    reply = _raw_ingest(plain_server.server_address[1],
                        b"Content-Length: " + b"9" * 5000 + b"\r\n")
    assert reply.startswith(b"HTTP/1.1 413 "), reply[:80]


def test_ingest_gives_up_on_a_body_that_never_arrives(plain_server, no_upstream,
                                                     monkeypatch):
    """A client that announces a body and trickles it must not hold a handler
    thread for the listener's 900 s; the client here waits 3 s at most."""
    import socket
    monkeypatch.setattr(proxy, "INGEST_READ_TIMEOUT", 0.2)
    head = (b"POST /__proxy/claude-usage HTTP/1.1\r\nHost: x\r\n"
            b"Origin: " + EXTENSION.encode() + b"\r\n"
            b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n")
    with socket.create_connection(("127.0.0.1", plain_server.server_address[1]),
                                  timeout=3) as sock:
        sock.sendall(head + b'{"org_id":')
        reply = b""
        while chunk := sock.recv(65536):
            reply += chunk
    assert reply.startswith(b"HTTP/1.1 408 "), reply[:80]
    assert proxy.Proxy.recorder.q.empty()


def test_ingest_deadline_covers_the_whole_body(plain_server, no_upstream, monkeypatch):
    """A byte every 0.1 s never trips a per-read timeout of 0.3 s; only a
    deadline on the body as a whole stops it. The client trickles until the
    server answers, and gives up after 3 s."""
    import select
    import socket
    monkeypatch.setattr(proxy, "INGEST_READ_TIMEOUT", 0.3)
    head = (b"POST /__proxy/claude-usage HTTP/1.1\r\nHost: x\r\n"
            b"Origin: " + EXTENSION.encode() + b"\r\n"
            b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n")
    with socket.create_connection(("127.0.0.1", plain_server.server_address[1]),
                                  timeout=3) as sock:
        sock.sendall(head)
        deadline = time.monotonic() + 3
        while not select.select([sock], [], [], 0.1)[0]:
            assert time.monotonic() < deadline, "server still waiting after 3 s"
            sock.sendall(b" ")
        reply = b""
        while chunk := sock.recv(65536):
            reply += chunk
    assert reply.startswith(b"HTTP/1.1 408 "), reply[:80]


def test_ingest_closes_quietly_when_the_client_hangs_up_mid_body(
        plain_server, no_upstream, capsys):
    """Nothing is written to a client that has gone: in the field the write
    lands on a dead socket and a BrokenPipe traceback lands in the launchd log.
    A half-close stands in for the hang-up so the test can see that no reply
    was attempted at all."""
    import socket
    head = (b"POST /__proxy/claude-usage HTTP/1.1\r\nHost: x\r\n"
            b"Origin: " + EXTENSION.encode() + b"\r\n"
            b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n")
    port = plain_server.server_address[1]
    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
        sock.sendall(head + b'{"org_id":')
        sock.shutdown(socket.SHUT_WR)
        reply = b""
        while chunk := sock.recv(65536):
            reply += chunk
    assert reply == b"", reply[:80]
    assert _ingest(port).status == 204, "the listener carries on"
    assert capsys.readouterr().err == ""


def test_ingest_requires_a_content_length(plain_server, no_upstream):
    reply = _raw_ingest(plain_server.server_address[1],
                        b"Transfer-Encoding: chunked\r\n")
    assert reply.startswith(b"HTTP/1.1 411 "), reply[:80]


@pytest.mark.parametrize("length", [b"-1", b"12abc", b"\xb2"])
def test_ingest_refuses_a_nonsense_content_length(plain_server, no_upstream, length):
    reply = _raw_ingest(plain_server.server_address[1],
                        b"Content-Length: " + length + b"\r\n")
    assert reply.startswith(b"HTTP/1.1 400 "), reply[:80]


@pytest.mark.parametrize("body", [
    pytest.param(b"not json", id="not-json"),
    pytest.param(b"[]", id="envelope-not-object"),
    pytest.param(json.dumps({"org_id": ORG, "observed_at": OBSERVED}).encode(),
                 id="payload-missing"),
    pytest.param(_envelope(payload=json.dumps(USAGE_PAYLOAD)), id="payload-a-string"),
    pytest.param(_envelope(payload={"hello": "world"}), id="payload-unrecognised"),
    pytest.param(_envelope(org_id=None), id="org-missing"),
    pytest.param(_envelope(org_id="../../etc"), id="org-bad-chars"),
    pytest.param(_envelope(org_id="a" * 65), id="org-too-long"),
    pytest.param(_envelope(observed_at=None), id="observed-missing"),
    pytest.param(_envelope(observed_at="yesterday"), id="observed-unparseable"),
    pytest.param(_envelope(observed_at=OBSERVED_TS * 1000), id="observed-a-number"),
    pytest.param(_envelope(observed_at=OBSERVED[:-5]), id="observed-no-timezone"),
    pytest.param(_envelope(observed_at="2020-01-01T00:00:00Z"), id="observed-stale"),
])
def test_ingest_refuses_what_is_not_a_usage_page(plain_server, no_upstream, body):
    response = _ingest(plain_server.server_address[1], body)
    assert response.status == 422, response.body
    assert proxy.Proxy.recorder.q.empty()


def test_usage_helpers_refuse_non_finite_numbers():
    for value in (float("nan"), float("inf"), float("-inf")):
        assert proxy._fraction(value) is None
        assert proxy._epoch(value) is None
    assert proxy._epoch(1e300) is None, "no datetime is that far out"
    assert proxy._epoch("9999-12-31T23:59:59Z") is not None


def _with_reset(reset: bytes) -> bytes:
    """An envelope whose five_hour reset is the raw JSON number `reset`."""
    payload = dict(USAGE_PAYLOAD, five_hour={"utilization": 10.0, "resets_at": 12345.5})
    return _envelope(payload=payload).replace(b"12345.5", reset)


@pytest.mark.parametrize("body", [
    pytest.param(_with_reset(b"1e999"), id="overflow"),
    pytest.param(_with_reset(b"-1e999"), id="negative-overflow"),
    pytest.param(_with_reset(b"NaN"), id="nan"),
    pytest.param(_with_reset(b"Infinity"), id="infinity"),
    pytest.param(_envelope(payload=dict(USAGE_PAYLOAD, seven_day={"utilization": 1e308})
                           ).replace(b"1e+308", b"1e999"), id="overflow-utilization"),
])
def test_ingest_refuses_non_finite_numbers(plain_server, no_upstream, body):
    assert any(lit in body for lit in (b"1e999", b"NaN", b"Infinity"))
    response = _ingest(plain_server.server_address[1], body)
    assert response.status == 422, response.body
    assert proxy.Proxy.recorder.q.empty()


def test_ingest_never_sends_cors_headers(plain_server, no_upstream):
    """No Access-Control-* on a success, a refusal or a preflight: that is
    what keeps a web page's JSON POST from ever being let through."""
    port = plain_server.server_address[1]
    responses = [_ingest(port), _ingest(port, headers={"Origin": "https://evil.example"}),
                 _ingest(port, method="OPTIONS", headers={
                     "Origin": "https://evil.example",
                     "Access-Control-Request-Method": "POST"})]
    assert [r.status for r in responses] == [204, 403, 501]
    assert not [n for r in responses for n, _ in r.getheaders()
                if n.lower().startswith("access-control-")]


def test_ingest_is_not_served_on_the_tls_listener(tls_server, no_upstream):
    """The TLS listener only ever stands in for api.anthropic.com. The route
    is still under /__proxy/, so it is answered locally, as the unknown path
    it is there, and never relayed."""
    response = _ingest(tls_server.server_address[1], tls=True)
    assert response.status == 404
    assert proxy.Proxy.recorder.q.empty()


@pytest.mark.parametrize("with_signal", [False, True])
def test_status_prints_the_latest_split_by_surface(tmp_path, monkeypatch, capsys,
                                                   with_signal):
    for name, stub in (("_launchctl", lambda *a: SimpleNamespace(returncode=1, stdout="")),
                       ("health", lambda port: None), ("hosts_entries", lambda: []),
                       ("cert_expiry", lambda path: None), ("stale_clients", lambda: []),
                       ("_listening", lambda port: False)):
        monkeypatch.setattr(proxy, name, stub)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    older = dict(USAGE_PAYLOAD, seven_day_breakdown={"rows": [
        {"key": "claude_code", "display_name": "Claude Code", "percent": 50},
        {"key": "chat", "display_name": "Chats", "percent": 50}]})
    newer = USAGE_PAYLOAD
    recorder = proxy.Recorder(tmp_path / "t.db")
    recorder.start()
    if with_signal:  # status used to stop early when there was none
        recorder.ratelimit({"anthropic-ratelimit-unified-7d-utilization": "0.4"})
    for ts, payload in ((OBSERVED_TS - 600, older), (OBSERVED_TS, newer)):
        row = proxy.parse_claude_usage(json.dumps(payload).encode(), ORG)
        recorder.claude_usage(dict(row, ts=ts))
    _settle(recorder)

    args = SimpleNamespace(port=1, tls_port=1, db=tmp_path / "t.db")
    assert proxy.status(args) == 0
    out = capsys.readouterr().out
    assert "this week by surface: Claude Code 79%, Chats 2%, Cowork 19%, Other 0%" in out
    assert "50%" not in out, "only the latest row is shown"
    assert "5h 10%  7d 72%  7d Fable 30%" in out


def _surface_line(tmp_path, capsys, breakdown) -> str:
    recorder = proxy.Recorder(tmp_path / "t.db")
    recorder.start()
    row = proxy.parse_claude_usage(
        json.dumps(dict(USAGE_PAYLOAD, seven_day_breakdown=breakdown)).encode(), ORG)
    recorder.claude_usage(dict(row, ts=OBSERVED_TS))
    _settle(recorder)
    proxy._print_claude_usage(tmp_path / "t.db")
    out = capsys.readouterr().out
    assert "5h 10%  7d 72%" in out
    return next((line for line in out.splitlines() if "by surface" in line), "")


def _usage_at(db: Path, ts: int):
    recorder = proxy.Recorder(db)
    recorder.start()
    row = proxy.parse_claude_usage(json.dumps(USAGE_PAYLOAD).encode(), ORG)
    recorder.claude_usage(dict(row, ts=ts))
    _settle(recorder)


def test_status_flags_a_usage_snapshot_older_than_the_signal(tmp_path, capsys):
    """A Time Machine restore dropped the unpacked extension and nothing said
    so for three days. Headers still arriving while snapshots stop is the tell."""
    _usage_at(tmp_path / "t.db", OBSERVED_TS - 2 * 86400)
    proxy._print_claude_usage(tmp_path / "t.db", signal_ts=OBSERVED_TS)
    out = capsys.readouterr().out
    assert "STALE: no claude.ai usage for 2.0 days of signal" in out
    assert "usage-extension" in out


def test_status_does_not_flag_a_recent_usage_snapshot(tmp_path, capsys):
    _usage_at(tmp_path / "t.db", OBSERVED_TS - 3600)
    proxy._print_claude_usage(tmp_path / "t.db", signal_ts=OBSERVED_TS)
    assert "STALE" not in capsys.readouterr().out


def test_status_says_the_fable_meter_is_half_the_week(tmp_path, capsys):
    _usage_at(tmp_path / "t.db", OBSERVED_TS)
    proxy._print_claude_usage(tmp_path / "t.db")
    assert "7d Fable 30% (of half the weekly cap)" in capsys.readouterr().out


def test_status_labels_a_surface_by_key_when_it_has_no_display_name(tmp_path, capsys):
    line = _surface_line(tmp_path, capsys, {"rows": [
        {"key": "claude_code", "percent": 90},
        {"key": "chat", "display_name": "Chats", "percent": 10},
        {"display_name": "Nameless", "percent": "x"}]})
    assert line == "  this week by surface: claude_code 90%, Chats 10%"


@pytest.mark.parametrize("breakdown", [
    [{"key": "chat", "percent": 5}], "rows", {"rows": "none"},
    {"rows": [None, 3, {"key": 1, "percent": True}]},
    {"rows": [{"surface": "claude_code", "utilization": 79}]},  # the spike's guess
])
def test_status_flags_a_breakdown_it_cannot_read(tmp_path, capsys, breakdown):
    """The page is not an API: whatever it turns into, status still prints,
    and says so rather than going quiet."""
    line = _surface_line(tmp_path, capsys, breakdown)
    assert line.startswith("  this week by surface: (unrecognised) "), line
    assert json.dumps(breakdown, separators=(",", ":"))[:20] in line
    assert len(line) < 120


def test_status_truncates_an_unreadable_breakdown(tmp_path, capsys):
    line = _surface_line(tmp_path, capsys, {"rows": [], "junk": "x" * 1000})
    assert line.startswith("  this week by surface: (unrecognised) {")
    assert line.endswith("...") and len(line) < 120


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


@pytest.mark.parametrize("model, per_mtok_in, per_mtok_out", [
    # $4 / $20 list, metered at 0.60x: fitted against Mythos (full list) on
    # the same meters and the Fable-scoped meter, then against the first
    # Max 5x week run to the cap (28 Oct 2026), which read 0.61x.
    ("claude-opus-5-5", 320_000, 1_600_000),
    ("claude-sonnet-5-5", 266_667, 1_333_334),  # $2 / $10
    ("claude-mythos-5-1", 1_333_334, 6_666_667),  # $10 / $50, as Fable
])
def test_credits_prices_the_5_5_models(model, per_mtok_in, per_mtok_out):
    assert proxy.credits(model, 1_000_000, 0, 0) == per_mtok_in
    assert proxy.credits(model, 0, 0, 1_000_000) == per_mtok_in, "cache writes at input rate"
    assert proxy.credits(model, 0, 1_000_000, 0) == per_mtok_out


def test_credits_prices_haiku_5_5_short_prompts_at_the_base_tier():
    # $0.10 / $0.50 for prompts up to 100K tokens
    assert proxy.credits("claude-haiku-5-5", 100_000, 0, 0) == 1_334
    assert proxy.credits("claude-haiku-5-5", 0, 1_000_000, 0) == 66_667
    assert proxy.credits("claude-haiku-5-5", 40_000, 0, 30_000, cache_read=30_000) == 934


def test_credits_prices_haiku_5_5_long_prompts_at_the_upper_tier():
    # $0.50 / $2.50 once the prompt, cache reads included, passes 100K tokens
    assert proxy.credits("claude-haiku-5-5", 100_001, 0, 0) == 6_667
    assert proxy.credits("claude-haiku-5-5", 1_500, 1_000_000, 0,
                         cache_read=150_000) == 333_434
    assert proxy.credits("claude-haiku-5-5", 60_000, 0, 50_000) == 7_334


def test_credits_between_counts_cache_reads_toward_the_long_prompt_tier(tmp_path):
    with proxy.connect(tmp_path / "r.db") as conn:
        conn.execute(
            "INSERT INTO requests (ts, model, input_tokens, output_tokens, "
            "cache_creation_tokens, cache_read_tokens) VALUES (?, ?, ?, ?, ?, ?)",
            (100, "claude-haiku-5-5", 15_000, 0, 0, 90_000))
        assert proxy._credits_between(conn, 0, 200) == 1_000  # $0.50 tier, not 200


def test_credits_ignores_cache_reads_for_untiered_models():
    assert proxy.credits("claude-opus-5-5", 1_000_000, 0, 0, cache_read=5_000_000) == 320_000


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", *sys.argv[1:]]))


def test_ca_trusted_means_macos_trusts_it_not_just_that_it_is_in_the_keychain(
        tmp_path, monkeypatch):
    # A restore can leave the CA in System.keychain with its trust settings
    # gone. Same subject as ours, never trusted: must not read as trusted.
    cert = tmp_path / "ca.pem"
    subprocess.run([proxy.OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(tmp_path / "ca.key"), "-out", str(cert),
                    "-subj", f"/CN={proxy.CA_SUBJECT}", "-days", "1"],
                   check=True, capture_output=True)
    monkeypatch.setattr(proxy, "CA_CERT", cert)
    assert proxy.ca_trusted() is False


def _local_ts(y, mo, d, h=12) -> int:
    from datetime import datetime
    return int(datetime(y, mo, d, h).timestamp())


@pytest.mark.parametrize("when, cap", [
    ((2026, 9, 1), 125_000_000),   # Max 20x, +50% weekly promotion
    ((2026, 10, 1), 104_166_666),  # Max 20x, permanent +25% from 14 Sep
    ((2026, 10, 17), 104_166_666),
    ((2026, 10, 19), 52_083_375),  # Max 5x from 18 Oct
])
def test_weekly_cap_follows_the_plan_and_promotion_in_force(when, cap):
    assert proxy.weekly_cap(_local_ts(*when)) == pytest.approx(cap, abs=1)


@pytest.mark.parametrize("day, flagged", [(16, True), (19, False)])
def test_shifts_judge_each_jump_against_the_cap_in_force_then(tmp_path, day, flagged):
    """3M credits explain a 10-point jump on a 52M week, not on a 104M one."""
    t0 = _local_ts(2026, 10, day)
    with proxy.connect(tmp_path / "s.db") as conn:
        for ts, util in ((t0, 0.10), (t0 + 3600, 0.20)):
            conn.execute("INSERT INTO ratelimit (ts, seven_d_utilization, seven_d_reset) "
                         "VALUES (?, ?, ?)", (ts, util, t0 + 86400))
        conn.execute("INSERT INTO requests (ts, model, input_tokens, output_tokens, "
                     "cache_creation_tokens, cache_read_tokens) VALUES (?, ?, ?, 0, 0, 0)",
                     (t0 + 60, "claude-opus-5", 4_500_000))  # 3,000,000 credits
        found, examined = proxy._shifts(conn)
    assert examined == 1
    assert bool(found) is flagged


def _shift_rows(conn, t0, readings):
    for dt, util in readings:
        conn.execute("INSERT INTO ratelimit (ts, seven_d_utilization, seven_d_reset) "
                     "VALUES (?, ?, ?)", (t0 + dt, util, t0 + 86400))


def test_shifts_ignore_stale_headers_from_concurrent_responses(tmp_path):
    """A long response finishing late carries the level from when it started.

    Seen 27 Oct 2026: 84 -> 81 -> 84 within one second, flagged twice.
    """
    t0 = _local_ts(2026, 10, 27)
    with proxy.connect(tmp_path / "s.db") as conn:
        _shift_rows(conn, t0, ((0, 0.84), (1, 0.81), (2, 0.84), (3, 0.85)))
        found, examined = proxy._shifts(conn)
    assert found == []
    assert examined == 2  # 0.84 -> 0.84 -> 0.85; the stale 0.81 is skipped


def test_shifts_restart_at_a_plan_switch_which_zeroes_the_meter(tmp_path):
    """The 18 Oct 2026 switch to Max 5x zeroed the 7d meter but kept the
    reset time, so the readings after it are not stale against those before."""
    t0 = _local_ts(2026, 10, 16)
    t1 = _local_ts(2026, 10, 19)
    with proxy.connect(tmp_path / "s.db") as conn:
        for ts, util in ((t0, 0.40), (t1, 0.10), (t1 + 3600, 0.20)):
            conn.execute("INSERT INTO ratelimit (ts, seven_d_utilization, seven_d_reset) "
                         "VALUES (?, ?, ?)", (ts, util, t1 + 86400))
        found, examined = proxy._shifts(conn)
    assert examined == 1
    assert [(f[2], f[3]) for f in found] == [(0.10, 0.20)]


def test_shifts_still_flag_a_jump_that_follows_a_stale_header(tmp_path):
    t0 = _local_ts(2026, 10, 27)
    with proxy.connect(tmp_path / "s.db") as conn:
        _shift_rows(conn, t0, ((0, 0.50), (1, 0.47), (3600, 0.70)))
        found, _ = proxy._shifts(conn)
    assert [(f[2], f[3]) for f in found] == [(0.50, 0.70)]


# --------------------------------------------------------------------------- #
# Prompt logging
# --------------------------------------------------------------------------- #

def _write_config(path: Path, value) -> None:
    """Write config.json and push its mtime forward, so a rewrite inside the
    same filesystem tick still reads as a change."""
    text = value if isinstance(value, str) else json.dumps(value)
    path.write_text(text)
    bump = getattr(_write_config, "bump", 0) + 1
    _write_config.bump = bump
    os.utime(path, ns=(time.time_ns() + bump * 1_000_000_000,) * 2)


@pytest.fixture
def config_now(monkeypatch):
    """Re-stat config.json on every request instead of every couple of seconds."""
    monkeypatch.setattr(proxy, "CONFIG_RECHECK", 0)


def test_config_is_off_when_the_file_is_missing(tmp_path, config_now):
    config = proxy.Config(tmp_path / "config.json")
    config.refresh()
    assert config.prompts.models == ()
    assert not config.prompts.wants("claude-sonnet-5")
    assert config.error is None


@pytest.mark.parametrize("value", [{}, {"log_prompts": {}}, {"log_prompts": {"models": []}},
                                   {"other": 1}])
def test_config_is_off_without_models(tmp_path, config_now, value):
    path = tmp_path / "config.json"
    _write_config(path, value)
    config = proxy.Config(path)
    config.refresh()
    assert config.prompts.models == ()
    assert config.error is None


def test_config_matches_exact_names_and_globs(tmp_path, config_now):
    path = tmp_path / "config.json"
    _write_config(path, {"log_prompts": {"models": ["claude-sonnet-5", "claude-haiku-*"],
                                         "max_bytes": 1234}, "future_key": True})
    config = proxy.Config(path)
    config.refresh()
    rules = config.prompts
    assert rules.models == ("claude-sonnet-5", "claude-haiku-*")
    assert rules.max_bytes == 1234
    assert rules.wants("claude-sonnet-5")
    assert not rules.wants("claude-sonnet-5[1m]"), "exact names match exactly"
    assert rules.wants("claude-haiku-5-5")
    assert not rules.wants("claude-opus-5-5")
    assert not rules.wants(None)
    assert config.error is None


def test_config_star_matches_every_model(tmp_path, config_now):
    path = tmp_path / "config.json"
    _write_config(path, {"log_prompts": {"models": ["*"]}})
    config = proxy.Config(path)
    config.refresh()
    assert config.prompts.wants("claude-opus-5-5")
    assert config.prompts.max_bytes == proxy.PROMPT_MAX_BYTES


@pytest.mark.parametrize("bad", [
    "{not json",
    "[]",
    json.dumps({"log_prompts": []}),
    json.dumps({"log_prompts": {"models": "claude-sonnet-5"}}),  # a string, not a list
    json.dumps({"log_prompts": {"models": [5]}}),
    json.dumps({"log_prompts": {"models": ["*"], "max_bytes": 0}}),
    json.dumps({"log_prompts": {"models": ["*"], "max_bytes": "big"}}),
    json.dumps({"log_prompts": {"models": ["*"], "max_bytes": True}}),
])
def test_bad_config_keeps_the_last_good_one_and_says_why(tmp_path, config_now, capsys, bad):
    path = tmp_path / "config.json"
    _write_config(path, {"log_prompts": {"models": ["claude-sonnet-5"]}})
    config = proxy.Config(path)
    config.refresh()
    _write_config(path, bad)
    config.refresh()
    assert config.prompts.models == ("claude-sonnet-5",), "last good config kept"
    assert config.error and str(path) in config.error
    assert "config" in capsys.readouterr().err

    config.refresh()  # unchanged since: no second complaint
    assert capsys.readouterr().err == ""

    _write_config(path, {"log_prompts": {"models": ["claude-opus-*"]}})
    config.refresh()
    assert config.prompts.models == ("claude-opus-*",)
    assert config.error is None, "a good config clears the error"


def test_bad_config_never_turns_logging_on(tmp_path, config_now):
    path = tmp_path / "config.json"
    _write_config(path, json.dumps({"log_prompts": {"models": ["*"], "max_bytes": -1}}))
    config = proxy.Config(path)
    config.refresh()
    assert config.prompts.models == ()
    assert config.error


def test_deleting_the_config_turns_logging_off(tmp_path, config_now):
    path = tmp_path / "config.json"
    _write_config(path, {"log_prompts": {"models": ["*"]}})
    config = proxy.Config(path)
    config.refresh()
    path.unlink()
    config.refresh()
    assert config.prompts.models == ()
    assert config.error is None


def test_config_is_restated_at_most_once_per_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "CONFIG_RECHECK", 3600)
    path = tmp_path / "config.json"
    config = proxy.Config(path)
    config.refresh()
    _write_config(path, {"log_prompts": {"models": ["*"]}})
    config.refresh()
    assert config.prompts.models == (), "inside the interval nothing is re-read"
    config.reload()
    assert config.prompts.models == ("*",), "reload() ignores the interval"


def test_sighup_reloads_the_config(tmp_path, monkeypatch):
    import signal
    monkeypatch.setattr(proxy, "CONFIG_RECHECK", 3600)
    path = tmp_path / "config.json"
    config = proxy.Config(path)
    config.refresh()
    _write_config(path, {"log_prompts": {"models": ["*"]}})
    previous = signal.getsignal(signal.SIGHUP)
    try:
        proxy.reload_on_sighup(config)
        os.kill(os.getpid(), signal.SIGHUP)
        assert _wait(lambda: config.prompts.models == ("*",))
    finally:
        signal.signal(signal.SIGHUP, previous)


class _MessagesUpstream:
    """An in-process stand-in for api.anthropic.com: answers every request
    with a small Messages reply carrying its own request-id, and remembers
    the bodies it was sent."""

    stats = {"pool_dropped": 0, "pool_stale_timeout": 0}

    def __init__(self, request_id: bool = True):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        outer = self
        self.bodies: list[bytes] = []
        self.ids = 0

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                outer.bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
                outer.ids += 1
                reply = json.dumps({
                    "model": "claude-sonnet-5", "stop_reason": "stop_sequence",
                    "usage": {"input_tokens": 3, "output_tokens": 9,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 70000}}).encode()
                # Chunked, as api.anthropic.com's Messages replies are.
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if request_id:
                    self.send_header("request-id", f"req_{outer.ids}")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b"%X\r\n%s\r\n0\r\n\r\n" % (len(reply), reply))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, args=(0.01,),
                         daemon=True).start()

    def send(self, method, path, headers, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1],
                                          timeout=5)
        conn.request(method, path, body=body, headers=headers)
        return conn, conn.getresponse()

    def give_back(self, conn):
        conn.close()


@pytest.fixture
def prompt_env(plain_server, config_now, tmp_path, monkeypatch):
    """A running proxy in front of _MessagesUpstream, its recorder started and
    its config.json at tmp_path/config.json (absent until a test writes it).

    The prompt is queued after the client has its whole response, so `relayed`
    is released once the handler is done with each request: _send waits on
    it, and only then is there anything for _settle to wait for."""
    upstream = _MessagesUpstream()
    proxy.Proxy.upstream = upstream
    proxy.Proxy.config = proxy.Config(tmp_path / "config.json")
    proxy.Proxy.recorder.start()
    relayed = threading.Semaphore(0)
    original = proxy.Proxy._relay

    def relay(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        finally:
            relayed.release()

    monkeypatch.setattr(proxy.Proxy, "_relay", relay)
    yield SimpleNamespace(port=plain_server.server_address[1], upstream=upstream,
                          config_path=tmp_path / "config.json",
                          recorder=proxy.Proxy.recorder, relayed=relayed)
    upstream.server.shutdown()
    upstream.server.server_close()


def _message(model="claude-sonnet-5", **extra) -> bytes:
    return json.dumps({"model": model, "max_tokens": 9, "stop_sequences": ["</x>"],
                       "system": [{"type": "text", "text": "You are a classifier."}],
                       "messages": [{"role": "user", "content": "hello"}],
                       **extra}).encode()


def _post(port, body: bytes, path="/v1/messages", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=body,
                 headers={"Content-Type": "application/json", **(headers or {})})
    response = conn.getresponse()
    response.body = response.read()
    conn.close()
    return response


def _send(env, body: bytes, **kwargs):
    """_post through prompt_env, returning once the proxy is done with it."""
    response = _post(env.port, body, **kwargs)
    assert env.relayed.acquire(timeout=5), "the proxy never finished the request"
    return response


def _prompt_rows(db: Path) -> list:
    with proxy.connect(db) as conn:
        return conn.execute("SELECT * FROM prompts ORDER BY id").fetchall()


def test_connect_adds_the_prompts_table_to_an_old_database(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:  # a database from before prompts existed
        conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL)")
    proxy.connect(db).close()
    with sqlite3.connect(db) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(prompts)")}
    assert {"id", "ts", "request_id", "path", "model", "bytes", "truncated",
            "encoding", "body"} <= columns


def test_a_matching_model_has_its_prompt_stored(prompt_env):
    _write_config(prompt_env.config_path, {"log_prompts": {"models": ["claude-sonnet-5"]}})
    sent = _message()
    response = _send(prompt_env, sent, path="/v1/messages?beta=true")
    assert response.status == 200
    _settle(prompt_env.recorder)

    rows = _prompt_rows(prompt_env.recorder.db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["request_id"] == "req_1"
    assert row["model"] == "claude-sonnet-5"
    assert row["path"] == "/v1/messages"
    assert row["bytes"] == len(sent)
    assert row["truncated"] == 0
    assert row["encoding"] == "zlib"
    assert proxy.prompt_body(row) == sent
    assert prompt_env.recorder.last_error is None


SECRETS = {"Authorization": "Bearer sk-ant-oat01-never-stored",
           "x-api-key": "sk-ant-api03-never-stored",
           "Cookie": "sessionKey=never-stored"}


def test_request_headers_are_never_stored(prompt_env):
    """Checked in every column of every prompts row, with the body decoded,
    and in the raw database and WAL files: in WAL mode a fresh commit sits in
    -wal, not in the main file."""
    _write_config(prompt_env.config_path, {"log_prompts": {"models": ["*"]}})
    _send(prompt_env, _message(), headers=SECRETS)
    _settle(prompt_env.recorder)

    db = prompt_env.recorder.db_path
    raw = b"".join(p.read_bytes() for p in (db, db.with_name(db.name + "-wal"))
                   if p.exists())
    rows = _prompt_rows(db)
    assert len(rows) == 1
    stored = []
    for row in rows:
        for column in row.keys():
            value = proxy.prompt_body(row) if column == "body" else row[column]
            stored.append(value if isinstance(value, bytes) else str(value).encode())
    stored = b"\n".join(stored)
    for value in SECRETS.values():
        secret = value.split("=")[-1].split()[-1].encode()
        assert secret not in stored
        assert secret not in raw


def test_a_prompt_joins_its_requests_row(prompt_env):
    _write_config(prompt_env.config_path, {"log_prompts": {"models": ["*"]}})
    _send(prompt_env, _message())
    _settle(prompt_env.recorder)
    with proxy.connect(prompt_env.recorder.db_path) as conn:
        joined = conn.execute("SELECT r.output_tokens, r.stop_reason FROM prompts p "
                              "JOIN requests r USING (request_id)").fetchall()
    assert [tuple(r) for r in joined] == [(9, "stop_sequence")]


@pytest.mark.parametrize("body", [
    _message(model="claude-opus-5-5"),
    b"not json at all",
    json.dumps({"messages": []}).encode(),       # no model
    json.dumps({"model": 5}).encode(),           # not a model name
    json.dumps(["claude-sonnet-5"]).encode(),
])
def test_other_requests_are_not_stored(prompt_env, body):
    _write_config(prompt_env.config_path, {"log_prompts": {"models": ["claude-sonnet-*"]}})
    assert _send(prompt_env, body).status == 200
    _settle(prompt_env.recorder)
    assert _prompt_rows(prompt_env.recorder.db_path) == []
    assert prompt_env.recorder.last_error is None


def test_nothing_is_queued_while_logging_is_off(prompt_env, monkeypatch):
    queued = []
    monkeypatch.setattr(prompt_env.recorder, "prompt",
                        lambda *a, **k: queued.append(a))
    _send(prompt_env, _message())
    _write_config(prompt_env.config_path, {"log_prompts": {"models": []}})
    _send(prompt_env, _message())
    assert queued == []


def test_editing_the_config_takes_effect_without_a_restart(prompt_env):
    _send(prompt_env, _message())                      # no config: off
    _write_config(prompt_env.config_path, {"log_prompts": {"models": ["claude-sonnet-5"]}})
    _send(prompt_env, _message())                      # on
    _write_config(prompt_env.config_path, {"log_prompts": {"models": ["claude-haiku-*"]}})
    _send(prompt_env, _message())                      # sonnet no longer matches
    _settle(prompt_env.recorder)
    assert [r["request_id"] for r in _prompt_rows(prompt_env.recorder.db_path)] == ["req_2"]


def test_a_long_prompt_is_stored_truncated(prompt_env):
    _write_config(prompt_env.config_path,
                  {"log_prompts": {"models": ["*"], "max_bytes": 100}})
    sent = _message(padding="x" * 5000)
    _send(prompt_env, sent)
    _settle(prompt_env.recorder)
    (row,) = _prompt_rows(prompt_env.recorder.db_path)
    assert row["bytes"] == len(sent)
    assert row["truncated"] == 1
    assert proxy.prompt_body(row) == sent[:100]
    assert prompt_env.upstream.bodies == [sent], "upstream still gets all of it"


def test_a_prompt_without_a_request_id_is_still_stored(prompt_env):
    upstream = proxy.Proxy.upstream = _MessagesUpstream(request_id=False)
    _write_config(prompt_env.config_path, {"log_prompts": {"models": ["*"]}})
    try:
        _send(prompt_env, _message())
    finally:
        upstream.server.shutdown()
        upstream.server.server_close()
    _settle(prompt_env.recorder)
    (row,) = _prompt_rows(prompt_env.recorder.db_path)
    assert row["request_id"] is None


def test_a_broken_prompt_log_never_breaks_the_response(prompt_env, monkeypatch):
    _write_config(prompt_env.config_path, {"log_prompts": {"models": ["*"]}})

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(prompt_env.recorder, "prompt", boom)
    response = _send(prompt_env, _message())
    assert response.status == 200
    assert json.loads(response.body)["stop_reason"] == "stop_sequence"
    assert "disk on fire" in prompt_env.recorder.last_error


def test_a_broken_config_check_never_breaks_the_response(prompt_env, monkeypatch):
    def boom():
        raise RuntimeError("stat exploded")

    monkeypatch.setattr(proxy.Proxy.config, "refresh", boom)
    response = _send(prompt_env, _message())
    assert response.status == 200
    assert "stat exploded" in prompt_env.recorder.last_error


def test_a_prompt_the_recorder_cannot_store_is_dropped_not_raised(tmp_path):
    recorder = proxy.Recorder(tmp_path / "t.db")
    recorder.start()
    recorder.prompt(b"\xff\xfe not json", "/v1/messages", "req_1",
                    proxy.PromptRules(("*",)))
    _settle(recorder)
    assert _prompt_rows(tmp_path / "t.db") == []
    assert recorder.last_error is None, "a non-JSON body is not an error"


def test_queued_prompts_are_capped_in_bytes(tmp_path, monkeypatch):
    """The queue holds whole bodies; a stalled writer must not let them pile up
    in memory without bound."""
    monkeypatch.setattr(proxy, "PROMPT_QUEUE_BYTES", 250)
    recorder = proxy.Recorder(tmp_path / "t.db")  # not started: nothing drains
    rules = proxy.PromptRules(("*",))
    for _ in range(3):
        recorder.prompt(b"x" * 100, "/v1/messages", None, rules)
    assert recorder.q.qsize() == 2
    assert recorder.dropped == 1
    recorder.start()
    _settle(recorder)
    recorder.prompt(b"x" * 100, "/v1/messages", None, rules)
    assert recorder.q.qsize() <= 1, "budget is given back as prompts are written"
    assert recorder.dropped == 1


def _health(port) -> dict:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/__proxy/health")
    body = json.load(conn.getresponse())
    conn.close()
    return body


def test_health_reports_the_prompt_log_in_force(prompt_env):
    assert _health(prompt_env.port)["prompt_log"] == {
        "models": [], "max_bytes": proxy.PROMPT_MAX_BYTES, "written": 0, "error": None}
    _write_config(prompt_env.config_path,
                  {"log_prompts": {"models": ["claude-sonnet-5"], "max_bytes": 10}})
    _send(prompt_env, _message())
    _settle(prompt_env.recorder)
    assert _health(prompt_env.port)["prompt_log"] == {
        "models": ["claude-sonnet-5"], "max_bytes": 10, "written": 1, "error": None}


def test_health_reports_a_config_that_did_not_load(prompt_env):
    _write_config(prompt_env.config_path, "{oops")
    error = _health(prompt_env.port)["prompt_log"]["error"]
    assert error and "config.json" in error


def _quiet_status(monkeypatch, info=None):
    for name, stub in (("_launchctl", lambda *a: SimpleNamespace(returncode=1, stdout="")),
                       ("health", lambda port: info), ("hosts_entries", lambda: []),
                       ("cert_expiry", lambda path: None), ("stale_clients", lambda: []),
                       ("_listening", lambda port: False)):
        monkeypatch.setattr(proxy, name, stub)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)


HEALTH = {"uptime_s": 5, "requests": 1, "errors": 0, "rows_written": 1, "rows_dropped": 0}


def _captured(db: Path, *bodies: tuple[bytes, str | None], max_bytes=proxy.PROMPT_MAX_BYTES):
    recorder = proxy.Recorder(db)
    recorder.start()
    for body, request_id in bodies:
        recorder.prompt(body, "/v1/messages", request_id,
                        proxy.PromptRules(("*",), max_bytes))
    _settle(recorder)


def test_status_shows_the_prompt_log_when_on(tmp_path, monkeypatch, capsys):
    _quiet_status(monkeypatch, dict(HEALTH, prompt_log={
        "models": ["claude-sonnet-5", "claude-haiku-*"], "max_bytes": 100,
        "written": 1, "error": None}))
    _captured(tmp_path / "t.db", (_message(), "req_1"), (_message(), "req_2"))
    proxy.status(SimpleNamespace(port=1, tls_port=1, db=tmp_path / "t.db"))
    out = capsys.readouterr().out
    assert "prompts : logging claude-sonnet-5, claude-haiku-* (2 captured)" in out


def test_status_is_silent_about_prompts_when_off(tmp_path, monkeypatch, capsys):
    _quiet_status(monkeypatch, dict(HEALTH, prompt_log={
        "models": [], "max_bytes": 100, "written": 0, "error": None}))
    proxy.status(SimpleNamespace(port=1, tls_port=1, db=tmp_path / "t.db"))
    assert "prompts" not in capsys.readouterr().out


def test_status_shows_a_config_error(tmp_path, monkeypatch, capsys):
    _quiet_status(monkeypatch, dict(HEALTH, prompt_log={
        "models": [], "max_bytes": 100, "written": 0,
        "error": "/x/config.json: ValueError: nope"}))
    proxy.status(SimpleNamespace(port=1, tls_port=1, db=tmp_path / "t.db"))
    out = capsys.readouterr().out
    assert "config  : /x/config.json: ValueError: nope" in out
    assert "last good" in out


def test_status_survives_a_database_from_before_prompts(tmp_path, monkeypatch, capsys):
    import sqlite3
    db = tmp_path / "old.db"
    proxy.connect(db).close()
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE prompts")
    _quiet_status(monkeypatch, dict(HEALTH, prompt_log={
        "models": ["*"], "max_bytes": 100, "written": 0, "error": None}))
    assert proxy.status(SimpleNamespace(port=1, tls_port=1, db=db)) == 0
    assert "prompts : logging * (0 captured)" in capsys.readouterr().out


def _request_row(db: Path, request_id: str, output_tokens: int, stop_reason: str):
    with proxy.connect(db) as conn:
        conn.execute("INSERT INTO requests (ts, request_id, model, output_tokens, "
                     "stop_reason) VALUES (?, ?, ?, ?, ?)",
                     (int(time.time()), request_id, "claude-sonnet-5", output_tokens,
                      stop_reason))


def test_prompts_lists_what_was_captured(tmp_path, capsys):
    db = tmp_path / "t.db"
    _captured(db, (_message(), "req_1"), (_message(model="claude-haiku-5-5"), "req_2"),
              (_message(), None))
    _request_row(db, "req_1", 9, "stop_sequence")
    assert proxy.main(["prompts", "--db", str(db)]) == 0
    lines = capsys.readouterr().out.splitlines()
    first = next(line for line in lines if "req_1" in line)
    assert "claude-sonnet-5" in first and "9" in first and "stop_sequence" in first
    assert f"{len(_message())}" in first.replace(",", "")
    assert any("req_2" in line and "claude-haiku-5-5" in line for line in lines)
    assert any("-" in line and "claude-sonnet-5" in line and "req_" not in line
               for line in lines), "a row without a request id is listed too"


def test_prompts_filters_by_model_glob_and_limit(tmp_path, capsys):
    db = tmp_path / "t.db"
    _captured(db, (_message(), "req_1"), (_message(model="claude-haiku-5-5"), "req_2"),
              (_message(), "req_3"))
    proxy.main(["prompts", "--db", str(db), "--model", "claude-son*"])
    out = capsys.readouterr().out
    assert "req_1" in out and "req_3" in out and "req_2" not in out
    proxy.main(["prompts", "--db", str(db), "--limit", "1"])
    out = capsys.readouterr().out
    assert "req_3" in out and "req_1" not in out, "the latest N"


def test_prompts_shows_the_system_prompt_and_last_messages(tmp_path, capsys):
    db = tmp_path / "t.db"
    body = _message(messages=[
        {"role": "user", "content": "first question, long gone"},
        {"role": "assistant", "content": [{"type": "text", "text": "an answer"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "tool output"},
            {"type": "text", "text": "is this turn done?"}]},
    ])
    _captured(db, (body, "req_1"))
    assert proxy.main(["prompts", "--db", str(db), "--request-id", "req_1"]) == 0
    out = capsys.readouterr().out
    assert "You are a classifier." in out
    assert "is this turn done?" in out and "tool output" in out
    assert "an answer" in out
    assert "</x>" in out, "stop sequences shown"
    assert "max_tokens" in out


def test_prompts_full_prints_the_whole_body_as_json(tmp_path, capsys):
    db = tmp_path / "t.db"
    _captured(db, (_message(), "req_1"))
    assert proxy.main(["prompts", "--db", str(db), "--request-id", "req_1",
                       "--full"]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(_message())


def test_prompts_prints_a_truncated_body_as_text(tmp_path, capsys):
    db = tmp_path / "t.db"
    body = _message(padding="x" * 500)
    _captured(db, (body, "req_1"), max_bytes=60)
    assert proxy.main(["prompts", "--db", str(db), "--request-id", "req_1"]) == 0
    out = capsys.readouterr().out
    assert "truncated" in out
    assert body[:60].decode() in out


def test_prompts_says_when_a_request_id_is_unknown(tmp_path, capsys):
    db = tmp_path / "t.db"
    _captured(db, (_message(), "req_1"))
    assert proxy.main(["prompts", "--db", str(db), "--request-id", "req_9"]) == 1
    assert "req_9" in capsys.readouterr().err


def test_prompts_reads_a_database_from_before_prompts(tmp_path, capsys):
    import sqlite3
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, ts INTEGER)")
    assert proxy.main(["prompts", "--db", str(db)]) == 0
    assert "no prompts captured" in capsys.readouterr().out


def test_serve_loads_the_config_and_reloads_it_on_sighup(tmp_path, monkeypatch, capsys):
    import signal
    monkeypatch.setattr(proxy, "CONFIG_RECHECK", 3600)
    path = tmp_path / "config.json"
    _write_config(path, {"log_prompts": {"models": ["claude-sonnet-5"]}})
    previous = signal.getsignal(signal.SIGHUP)
    try:
        config = proxy.load_config(path)
        assert config.prompts.models == ("claude-sonnet-5",), "read at startup"
        assert "logging prompts for claude-sonnet-5" in capsys.readouterr().err
        _write_config(path, {"log_prompts": {"models": []}})
        os.kill(os.getpid(), signal.SIGHUP)
        assert _wait(lambda: config.prompts.models == ())
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_serve_says_nothing_about_prompts_when_off(tmp_path, capsys):
    import signal
    previous = signal.getsignal(signal.SIGHUP)
    try:
        proxy.load_config(tmp_path / "config.json")
    finally:
        signal.signal(signal.SIGHUP, previous)
    assert capsys.readouterr().err == ""


def _head_body(head: dict, tail: bytes) -> bytes:
    """A JSON object whose first keys are `head`, followed by `tail` verbatim
    where the rest of the object would be."""
    return json.dumps(head).encode()[:-1] + b", " + tail


@pytest.mark.parametrize("body, model", [
    (_message(), "claude-sonnet-5"),
    # Found in the head even though the rest of the body is not JSON at all,
    # which proves the rest was never parsed.
    (_head_body({"model": "claude-sonnet-5"}, b'"messages": [{"oops' + b"x" * 200_000),
     "claude-sonnet-5"),
    # Not first, but within the head.
    (_head_body({"max_tokens": 9, "system": "s" * 1000, "model": "claude-sonnet-5"},
                b'"messages": []}'), "claude-sonnet-5"),
    # Whitespace JSON allows.
    (b'{ "stream" : true ,\n  "model"\t:  "claude-sonnet-5" }', "claude-sonnet-5"),
    # Escapes in the name are decoded, as json would.
    (b'{"model": "claude-sonnet-5\\u005b1m]"}', "claude-sonnet-5[1m]"),
    # Only a top-level model counts: one inside a message or a tool input is
    # data, not the request's model.
    (_head_body({"messages": [{"role": "user", "content": [
        {"type": "tool_use", "name": "x", "input": {"model": "claude-sonnet-5"}}]}]},
        b'"max_tokens": 9}'), None),
    (b'{"content": "say \\"model\\": \\"claude-sonnet-5\\""}', None),
    # Past the head: skipped, not found by parsing the whole thing.
    (_head_body({"messages": "x" * (proxy.PROMPT_HEAD + 10)},
                b'"model": "claude-sonnet-5"}'), None),
    (b'{"model": 5}', None),
    (b'["model", "claude-sonnet-5"]', None),
    (b"not json", None),
    (b"\xff\xfe{", None),
    (b"", None),
], ids=["first", "tail-not-json", "not-first", "whitespace", "escaped", "tool-input",
        "inside-a-string", "past-the-head", "not-a-string", "array", "not-json",
        "not-utf8", "empty"])
def test_request_model_comes_from_the_head_of_the_body(body, model):
    assert proxy.request_model(body) == model


def test_matching_a_prompt_never_parses_the_whole_body(monkeypatch):
    """A writer thread json.loads-ing a 4 MB body holds the GIL for tens of
    milliseconds, stalling every streaming relay meanwhile."""
    big = _head_body({"model": "claude-sonnet-5"},
                     b'"messages": "' + b"x" * 4_000_000 + b'"}')
    real = json.loads

    def loads(text, *args, **kwargs):
        assert len(text) <= proxy.PROMPT_HEAD, f"parsed {len(text):,} bytes"
        return real(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)
    row = proxy.prompt_row(1, big, "/v1/messages", None, proxy.PromptRules(("*",)))
    assert row["model"] == "claude-sonnet-5"
    assert row["bytes"] == len(big)


def test_a_stat_error_is_reported_once_not_every_recheck(tmp_path, config_now, capsys):
    path = tmp_path / "config.json"
    _write_config(path, {"log_prompts": {"models": ["claude-sonnet-5"]}})
    config = proxy.Config(path)
    config.refresh()

    class Unreadable(type(path)):
        def stat(self, *args, **kwargs):
            raise PermissionError(13, "Permission denied")

    config.path = Unreadable(path)
    for _ in range(3):
        config.refresh()
    err = capsys.readouterr().err
    assert err.count("Permission denied") == 1, err
    assert "Permission denied" in config.error
    assert config.prompts.models == ("claude-sonnet-5",), "last good config kept"

    config.path = path  # readable again
    config.refresh()
    assert config.error is None
    config.path = Unreadable(path)
    config.refresh()
    assert capsys.readouterr().err.count("Permission denied") == 1, \
        "the same error after a recovery is news again"


def test_an_exact_name_matches_even_with_glob_characters():
    rules = proxy.PromptRules(("claude-sonnet-5[1m]",))
    assert rules.wants("claude-sonnet-5[1m]")
    assert not rules.wants("claude-sonnet-5")
    assert rules.wants("claude-sonnet-5m"), "for anything else it is still a glob"


def test_prompts_filters_by_an_exact_name_with_glob_characters(tmp_path, capsys):
    db = tmp_path / "t.db"
    _captured(db, (_message(model="claude-sonnet-5[1m]"), "req_1"),
              (_message(), "req_2"))
    proxy.main(["prompts", "--db", str(db), "--model", "claude-sonnet-5[1m]"])
    out = capsys.readouterr().out
    assert "req_1" in out and "req_2" not in out


# --------------------------------------------------------------------------- #
# Response framing
# --------------------------------------------------------------------------- #

class _CannedUpstream:
    """An upstream that answers its Nth request with replies[N], byte for byte,
    so a test controls the framing exactly. One request per connection, as
    the fake send() below opens a fresh one each time. A connection is closed
    `linger` seconds after its reply (0: at once, as a dying upstream would),
    and `given_back` lists the connections the proxy returned to the pool."""

    stats = {"pool_dropped": 0, "pool_stale_timeout": 0}

    def __init__(self, replies: list[bytes], linger: float = 2):
        import socket
        self.replies = list(replies)
        self.linger = linger
        self.given_back: list = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        for reply in self.replies:
            conn, _ = self.sock.accept()
            head = b""
            while b"\r\n\r\n" not in head:
                head += conn.recv(65536)
            headers, _, body = head.partition(b"\r\n\r\n")
            for line in headers.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    while len(body) < int(line.split(b":")[1]):
                        body += conn.recv(65536)
            conn.sendall(reply)
            if self.linger:
                threading.Timer(self.linger, conn.close).start()  # keep-alive
            else:
                conn.close()

    def send(self, method, path, headers, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.sock.getsockname()[1],
                                          timeout=5)
        conn.request(method, path, body=body, headers=headers)
        return conn, conn.getresponse()

    def give_back(self, conn):
        self.given_back.append(conn)
        conn.close()


OK_AFTER = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: 11\r\n\r\n{\"next\": 1}")


def _keepalive_pair(port, first: tuple, second: tuple = ("POST", "/v1/messages")):
    """Two requests on one client connection with a short read timeout: the
    first response, then the second, which only arrives intact if the first
    was framed exactly."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    method, path = first
    conn.request(method, path, body=b"{}" if method == "POST" else None)
    response = conn.getresponse()
    body = response.read()
    sock = conn.sock
    conn.request(*second, body=b"{}")
    after = conn.getresponse()
    after_body = after.read()
    assert conn.sock is sock, "the client had to reconnect"
    conn.close()
    return response, body, after, after_body


def test_a_fixed_length_reply_keeps_its_content_length(plain_server):
    """Content-Length used to be dropped with nothing put in its place, so a
    non-chunked reply had no framing at all on a keep-alive connection and the
    client sat waiting for a close that never came."""
    reply = b'{"input_tokens": 12}'
    proxy.Proxy.upstream = _CannedUpstream([
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: %d\r\n\r\n%s" % (len(reply), reply), OK_AFTER])
    response, body, after, after_body = _keepalive_pair(
        plain_server.server_address[1], ("POST", "/v1/messages/count_tokens"))
    assert body == reply
    assert response.getheader("Content-Length") == str(len(reply))
    assert response.getheader("Transfer-Encoding") is None
    assert after_body == b'{"next": 1}'


def test_a_chunked_reply_with_a_content_length_too_is_relayed_chunked(plain_server):
    """Transfer-Encoding wins over Content-Length (RFC 9112 6.3), and the relay
    re-chunks what it reads, so the stale length must not go along with it."""
    proxy.Proxy.upstream = _CannedUpstream([
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 999\r\n\r\n"
        b"5\r\nhello\r\n0\r\n\r\n", OK_AFTER])
    response, body, after, after_body = _keepalive_pair(
        plain_server.server_address[1], ("POST", "/v1/messages"))
    assert body == b"hello"
    assert response.getheader("Content-Length") is None
    assert after_body == b'{"next": 1}'


def _raw_pair(port, first: bytes) -> tuple[bytes, bytes]:
    """Send `first` and then a POST on one raw socket; return the first
    response's head and every byte that follows it. A client that knows the
    first response has no body reads the next one from right there, so stray
    bytes in between are a corrupted stream to it."""
    import socket
    sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    sock.sendall(first)
    data = b""
    while b"\r\n\r\n" not in data:
        data += sock.recv(65536)
    head, _, rest = data.partition(b"\r\n\r\n")
    sock.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
    try:
        while not rest.endswith(b'{"next": 1}'):
            chunk = sock.recv(65536)
            if not chunk:
                break
            rest += chunk
    except TimeoutError:
        pass
    sock.close()
    return head, rest


@pytest.mark.parametrize("status", [b"204 No Content", b"304 Not Modified"])
def test_a_bodiless_status_is_relayed_without_a_body(plain_server, status):
    proxy.Proxy.upstream = _CannedUpstream([b"HTTP/1.1 " + status + b"\r\n\r\n", OK_AFTER])
    head, rest = _raw_pair(plain_server.server_address[1],
                           b"POST /v1/files/x HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
    assert head.startswith(b"HTTP/1.1 " + status)
    assert b"transfer-encoding" not in head.lower()
    assert rest.startswith(b"HTTP/1.1 200"), rest[:40]
    assert rest.endswith(b'{"next": 1}')


@pytest.mark.parametrize("length", [b"Content-Length: 42\r\n", b""])
def test_a_head_reply_is_relayed_without_a_body(plain_server, length):
    proxy.Proxy.upstream = _CannedUpstream([
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + length + b"\r\n",
        OK_AFTER])
    head, rest = _raw_pair(plain_server.server_address[1],
                           b"HEAD /v1/models HTTP/1.1\r\nHost: x\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")
    if length:
        assert b"Content-Length: 42" in head, "a HEAD keeps the length of what GET would send"
    assert b"transfer-encoding" not in head.lower()
    assert rest.startswith(b"HTTP/1.1 200"), rest[:40]



def test_a_reply_cut_short_of_its_content_length_closes_the_client(plain_server):
    """The length has already gone to the client, so a reply that ends early
    can only be signalled by closing: left open, the client would wait out
    the listener's 900 s for bytes that are never coming."""
    upstream = proxy.Proxy.upstream = _CannedUpstream(
        [b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"x" * 40], linger=0)
    conn = http.client.HTTPConnection("127.0.0.1", plain_server.server_address[1],
                                      timeout=3)
    conn.request("POST", "/v1/messages", body=b"{}")
    response = conn.getresponse()
    t0 = time.monotonic()
    with pytest.raises(http.client.IncompleteRead) as cut:
        response.read()
    assert time.monotonic() - t0 < 1.5, "the client waited instead of seeing EOF"
    assert cut.value.partial == b"x" * 40
    conn.close()
    assert upstream.given_back == [], "a dead upstream connection went back to the pool"


def _raw_until_eof(port, request: bytes, timeout=3) -> tuple[bytes, float]:
    """Everything the proxy sends for `request` until it closes, and how long
    that took. A TimeoutError means it never closed."""
    import socket
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock.sendall(request)
    t0 = time.monotonic()
    data = b""
    while chunk := sock.recv(65536):
        data += chunk
    sock.close()
    return data, time.monotonic() - t0


def test_an_informational_reply_never_goes_back_to_the_pool(plain_server):
    """http.client hands back a 103 as if it were the final response, leaving
    the real one unread on the upstream socket: pooled, that connection would
    answer the next request with the previous one's reply."""
    upstream = proxy.Proxy.upstream = _CannedUpstream([
        b"HTTP/1.1 103 Early Hints\r\nLink: </x>; rel=preload\r\n\r\n"
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok", OK_AFTER])
    port = plain_server.server_address[1]
    data, took = _raw_until_eof(
        port, b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
    assert data.startswith(b"HTTP/1.1 103")
    assert took < 1.5, "the client connection was left open after a 103"
    assert upstream.given_back == []

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request("POST", "/v1/messages", body=b"{}")
    response = conn.getresponse()
    assert (response.status, response.read()) == (200, b'{"next": 1}')
    conn.close()


def test_a_close_delimited_reply_is_relayed_chunked(plain_server):
    proxy.Proxy.upstream = _CannedUpstream(
        [b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nhello"], linger=0)
    import socket
    sock = socket.create_connection(("127.0.0.1", plain_server.server_address[1]),
                                    timeout=3)
    sock.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
    data = b""
    while not data.endswith(b"0\r\n\r\n"):
        data += sock.recv(65536)
    sock.close()
    head, _, rest = data.partition(b"\r\n\r\n")
    assert b"transfer-encoding: chunked" in head.lower()
    assert b"connection: close" not in head.lower(), "upstream's hop header stays there"
    assert rest == b"5\r\nhello\r\n0\r\n\r\n"


def test_a_streamed_reply_arrives_whole_and_the_connection_is_reused(plain_server):
    events = [b"event: message_start\ndata: {}\n\n",
              b"event: content_block_delta\ndata: {\"text\": \"hi\"}\n\n",
              b"event: message_delta\ndata: {}\n\n",
              b"event: message_stop\ndata: {}\n\n"]
    stream = b"".join(b"%X\r\n%s\r\n" % (len(e), e) for e in events) + b"0\r\n\r\n"
    proxy.Proxy.upstream = _CannedUpstream([
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n" + stream, OK_AFTER])
    response, body, after, after_body = _keepalive_pair(
        plain_server.server_address[1], ("POST", "/v1/messages"))
    assert response.getheader("Transfer-Encoding") == "chunked"
    assert response.getheader("Content-Type") == "text/event-stream"
    assert body == b"".join(events)
    assert after_body == b'{"next": 1}'


def test_an_error_reply_is_relayed_exactly(plain_server):
    error = (b'{"type":"error","error":{"type":"invalid_request_error",'
             b'"message":"max_tokens: field required"},"request_id":"req_x"}')
    proxy.Proxy.upstream = _CannedUpstream([
        b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\n"
        b"request-id: req_x\r\nContent-Length: %d\r\n\r\n%s" % (len(error), error),
        OK_AFTER])
    response, body, after, after_body = _keepalive_pair(
        plain_server.server_address[1], ("POST", "/v1/messages"))
    assert response.status == 400
    assert body == error
    assert response.getheader("Content-Length") == str(len(error))
    assert response.getheader("request-id") == "req_x"
    assert after_body == b'{"next": 1}'


@pytest.fixture
def cert_dir(tmp_path, monkeypatch):
    """ensure_certs() writing into a scratch directory, never the live one."""
    d = tmp_path / "tls"
    monkeypatch.setattr(proxy, "CERT_DIR", d)
    monkeypatch.setattr(proxy, "CA_KEY", d / "ca.key")
    monkeypatch.setattr(proxy, "CA_CERT", d / "ca.pem")
    monkeypatch.setattr(proxy, "LEAF_KEY", d / "leaf.key")
    monkeypatch.setattr(proxy, "LEAF_CERT", d / "leaf.pem")
    proxy.ensure_certs()
    return d


def _verify(ca: Path, cert: Path) -> subprocess.CompletedProcess:
    return subprocess.run([proxy.OPENSSL, "verify", "-CAfile", str(ca), str(cert)],
                          capture_output=True, text=True)


def _sign_with_ca(d: Path, host: str) -> Path:
    """A server cert for `host`, signed by our CA as a stolen key could."""
    key, csr, crt, ext = (d / f"{host}.{s}" for s in ("key", "csr", "pem", "cnf"))
    ext.write_text(f"[v3]\nsubjectAltName = DNS:{host}\n")
    subprocess.run([proxy.OPENSSL, "req", "-new", "-newkey", "ec",
                    "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={host}"],
                   check=True, capture_output=True)
    subprocess.run([proxy.OPENSSL, "x509", "-req", "-in", str(csr),
                    "-CA", str(d / "ca.pem"), "-CAkey", str(d / "ca.key"),
                    "-CAcreateserial", "-CAserial", str(d / "ca.srl"),
                    "-days", "1", "-out", str(crt), "-extfile", str(ext),
                    "-extensions", "v3"],
                   check=True, capture_output=True)
    return crt


def test_the_ca_still_vouches_for_its_own_leaf(cert_dir):
    result = _verify(cert_dir / "ca.pem", cert_dir / "leaf.pem")
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_ca_cannot_vouch_for_any_other_site(cert_dir):
    # The CA sits in the System keychain as a trusted root. Name constraints
    # keep a leaked ca.key from minting a cert the machine would accept for,
    # say, a bank.
    forged = _sign_with_ca(cert_dir, "www.example.com")
    result = _verify(cert_dir / "ca.pem", forged)
    assert result.returncode != 0
    assert "permitted subtree violation" in result.stdout + result.stderr


# --------------------------------------------------------------------------- #
# status: the client-eye roundtrip
# --------------------------------------------------------------------------- #

API_401 = (b'{"type":"error","error":{"type":"authentication_error",'
           b'"message":"x-api-key header is required"},"request_id":"req_1"}')
CF_522 = (b"<!DOCTYPE html><html><head><title>api.anthropic.com | 522: "
          b"Connection timed out</title></head><body>...</body></html>")


def _reply(status: int, body: bytes, reason: str = "X", **headers: str) -> bytes:
    head = [f"HTTP/1.1 {status} {reason}", f"Content-Length: {len(body)}",
            "Connection: close"]
    head += [f"{name.replace('_', '-')}: {value}" for name, value in headers.items()]
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


def _canned_tls(cert_dir, reply: bytes | None, seen: list | None = None) -> int:
    """A one-shot TLS server with the proxy's own leaf, as the client meets
    it. Records the request it got in `seen`; `reply=None` never answers."""
    import socket

    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_dir / "leaf.pem", cert_dir / "leaf.key")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()

    def run():
        conn, _ = sock.accept()
        try:
            conn = tls.wrap_socket(conn, server_side=True)
        except ssl.SSLError:
            return  # the client refused the certificate
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(65536)
            head, _, rest = data.partition(b"\r\n\r\n")
            length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n")
                           if line.lower().startswith(b"content-length:")), 0)
            while len(rest) < length:
                rest += conn.recv(65536)
            if seen is not None:
                seen.append((head.decode(), rest))
            if reply is None:
                time.sleep(3)
                return
            conn.sendall(reply)

    threading.Thread(target=run, daemon=True).start()
    return sock.getsockname()[1]


def test_roundtrip_passes_on_the_apis_own_401(cert_dir):
    seen = []
    port = _canned_tls(cert_dir, _reply(401, API_401, request_id="req_1",
                                        cf_ray="abc-SYD", server="cloudflare"), seen)
    ok, line = proxy.roundtrip(port)
    assert ok, line
    assert "401 authentication_error" in line and "req_1" in line
    assert "ms" in line


def test_roundtrip_speaks_as_a_client_without_credentials(cert_dir):
    seen = []
    port = _canned_tls(cert_dir, _reply(401, API_401), seen)
    proxy.roundtrip(port)
    head, body = seen[0]
    request_line, *lines = head.split("\r\n")
    assert request_line == "POST /v1/messages HTTP/1.1"
    names = {line.split(":")[0].lower(): line.split(":", 1)[1].strip() for line in lines}
    assert names["host"] == proxy.UPSTREAM_HOST
    assert "anthropic-version" in names
    assert not any(proxy.SECRET.search(name) for name in names)
    assert json.loads(body) == {}


def test_roundtrip_names_a_cloudflare_error_page(cert_dir):
    port = _canned_tls(cert_dir, _reply(522, CF_522, cf_ray="abc-SYD",
                                        server="cloudflare",
                                        content_type="text/html"))
    ok, line = proxy.roundtrip(port)
    assert not ok
    assert "Cloudflare" in line and "522" in line
    assert "Connection timed out" in line
    assert "abc-SYD" in line


def test_roundtrip_names_a_cloudflare_challenge(cert_dir):
    page = b"<html><head><title>Just a moment...</title></head></html>"
    port = _canned_tls(cert_dir, _reply(403, page, cf_ray="abc-SYD",
                                        server="cloudflare"))
    ok, line = proxy.roundtrip(port)
    assert not ok
    assert "Cloudflare" in line and "403" in line and "Just a moment..." in line


def test_roundtrip_reports_an_api_error_that_is_not_auth(cert_dir):
    body = b'{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
    port = _canned_tls(cert_dir, _reply(529, body, request_id="req_2",
                                        cf_ray="abc-SYD"))
    ok, line = proxy.roundtrip(port)
    assert not ok
    assert "529 overloaded_error" in line and "Overloaded" in line


def test_roundtrip_blames_the_proxy_for_its_own_502(cert_dir):
    # What Proxy.send_error(502) says when upstream is unreachable: no
    # Cloudflare headers, because nothing got as far as Cloudflare, and the
    # message in the reason phrase; the page itself is a generic template.
    page = b"<html><head><title>Error response</title></head></html>"
    port = _canned_tls(cert_dir, _reply(502, page, reason="upstream unreachable",
                                        server="BaseHTTP/0.6 Python/3.13"))
    ok, line = proxy.roundtrip(port)
    assert not ok
    assert "proxy" in line and "502" in line and "upstream unreachable" in line


def test_roundtrip_reports_nothing_listening(cert_dir):
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    ok, line = proxy.roundtrip(port)
    assert not ok
    assert "refused" in line


def test_roundtrip_reports_a_certificate_the_client_would_reject(cert_dir, tmp_path,
                                                                 monkeypatch):
    port = _canned_tls(cert_dir, _reply(401, API_401))
    other = tmp_path / "other.pem"
    subprocess.run([proxy.OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(tmp_path / "other.key"), "-out", str(other),
                    "-subj", "/CN=other", "-days", "1"], check=True, capture_output=True)
    monkeypatch.setattr(proxy, "CA_CERT", other)
    ok, line = proxy.roundtrip(port)
    assert not ok
    assert "certificate" in line.lower()


def test_roundtrip_gives_up_on_a_silent_server(cert_dir):
    port = _canned_tls(cert_dir, None)
    ok, line = proxy.roundtrip(port, timeout=0.5)
    assert not ok
    assert "no reply" in line


def test_status_prints_the_roundtrip_when_tls_is_listening(tmp_path, monkeypatch,
                                                           capsys):
    _quiet_status(monkeypatch)
    monkeypatch.setattr(proxy, "_listening", lambda port: True)
    monkeypatch.setattr(proxy, "roundtrip",
                        lambda port: (False, "Cloudflare 522 page, not the API"))
    proxy.status(SimpleNamespace(port=1, tls_port=1, db=tmp_path / "t.db"))
    assert "trip    : FAILED: Cloudflare 522 page, not the API" in capsys.readouterr().out


def test_status_skips_the_roundtrip_when_tls_is_down(tmp_path, monkeypatch, capsys):
    _quiet_status(monkeypatch)
    monkeypatch.setattr(proxy, "roundtrip", lambda port: pytest.fail("tried anyway"))
    proxy.status(SimpleNamespace(port=1, tls_port=1, db=tmp_path / "t.db"))
    assert "trip" not in capsys.readouterr().out
