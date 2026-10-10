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
    ("claude-opus-5-5", 533_334, 2_666_667),    # $4 / $20
    ("claude-sonnet-5-5", 266_667, 1_333_334),  # $2 / $10
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
    assert proxy.credits("claude-opus-5-5", 1_000_000, 0, 0, cache_read=5_000_000) == 533_334


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", *sys.argv[1:]]))
