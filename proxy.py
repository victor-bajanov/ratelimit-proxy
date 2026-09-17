#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Always-on logging proxy for api.anthropic.com.

Forwards every request verbatim and records two things to SQLite:

  * ratelimit  - the `anthropic-ratelimit-unified-*` response headers, which are
                 the authoritative 5h / 7d plan utilisation signal. Deduped, so
                 a row appears only when the signal actually moves.
  * requests   - per-request model + token usage, sniffed out of the response
                 without disturbing the stream.

Credentials (Authorization / x-api-key / cookies) are forwarded but never
logged, and only loopback clients are served.

It gets into the path by *impersonating* api.anthropic.com rather than by
being pointed at: an /etc/hosts entry sends the name to 127.0.0.1, and the
proxy answers on :443 with a certificate signed by a locally trusted CA. That
matters because Claude Code disables Remote Control whenever ANTHROPIC_BASE_URL
names a host other than api.anthropic.com; here the base URL stays untouched.

The catch is that the proxy cannot use the system resolver to find its own
upstream, since /etc/hosts now points the name at the proxy. It asks the
configured nameservers over UDP directly and connects to the real address with
SNI and Host intact.

Design rule: fail open. This process sits in front of every Claude Code
request, so a logging bug must never cost you a session. Every record path is
wrapped, writes go to a bounded queue drained by a background thread, and a
full queue drops samples rather than blocking the proxy.

    uv run proxy.py serve                 # run in foreground
    uv run proxy.py cert                  # create / renew the CA and leaf
    uv run proxy.py trust                 # print the keychain + hosts commands
    uv run proxy.py install               # install + start the launchd agent
    uv run proxy.py status                # agent state, health, latest signal
    uv run proxy.py history --days 7      # utilisation over time
    uv run proxy.py usage --days 7        # tokens + credits by model

The plain-HTTP listener on 127.0.0.1:8787 stays up as a fallback, so
ANTHROPIC_BASE_URL still works if the intercept is ever backed out.
"""
from __future__ import annotations

import argparse
import collections
import http.client
import ipaddress
import json
import math
import os
import plistlib
import queue
import re
import resource
import secrets
import select
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UPSTREAM_HOST = "api.anthropic.com"
DEFAULT_PORT = 8787
DEFAULT_TLS_PORT = 443
LABEL = "id.bajanov.claude-ratelimit-proxy"
DATA_DIR = Path(os.environ.get("CLAUDE_RATELIMIT_DIR",
                               Path.home() / ".claude" / "ratelimit-proxy"))
DB_PATH = DATA_DIR / "ratelimit.db"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
SELF = Path(__file__).resolve()

# TLS material for impersonating the upstream host.
CERT_DIR = DATA_DIR / "tls"
CA_KEY, CA_CERT = CERT_DIR / "ca.key", CERT_DIR / "ca.pem"
LEAF_KEY, LEAF_CERT = CERT_DIR / "leaf.key", CERT_DIR / "leaf.pem"
CA_SUBJECT = "claude-ratelimit-proxy local CA"
CA_DAYS = 3650
# Apple caps TLS server certs at 398 days even under a locally added root, so
# the leaf is short-lived and renewed automatically on startup.
LEAF_DAYS = 397
RENEW_WITHIN = 30 * 86400
HOSTS_PATH = Path("/etc/hosts")
HOSTS_MARK = "# claude-ratelimit-proxy"
SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"
OPENSSL = shutil.which("openssl") or "/usr/bin/openssl"

# Headers we never write anywhere, even by accident.
SECRET = re.compile(r"(authorization|api-key|cookie|token|secret)", re.I)
# Headers worth keeping: anything that smells like quota signal.
KEEP = re.compile(r"(ratelimit|rate-limit|unified|quota|retry-after)", re.I)
# Per-hop headers, plus framing headers we regenerate ourselves. accept-encoding
# is dropped so upstream replies in identity and usage stays sniffable.
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "host",
       "accept-encoding", "content-length"}

# Credits per Mtok, mirroring sched.py: (input, output).
PRICE = {"claude-fable-5": (10, 50), "claude-fable-5-1": (10, 50),
         "claude-opus-5": (5, 25), "claude-opus-4-8": (5, 25),
         "claude-opus-4-7": (5, 25), "claude-sonnet-5": (2, 10),
         "claude-sonnet-4-6": (3, 15), "claude-haiku-4-5-20251001": (1, 5),
         "claude-haiku-4-5": (1, 5)}


def credits(model: str, inp: int, out: int, cache_write: int) -> int:
    """Plan credits for one request. Unknown models cost nothing we can price."""
    price = PRICE.get(model)
    if not price:
        return 0
    pin, pout = price
    return math.ceil((inp + cache_write) * pin * 2 / 15 + out * pout * 2 / 15)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS ratelimit (
    id                   INTEGER PRIMARY KEY,
    ts                   INTEGER NOT NULL,
    status               TEXT,
    five_h_status        TEXT,
    five_h_utilization   REAL,
    five_h_reset         INTEGER,
    seven_d_status       TEXT,
    seven_d_utilization  REAL,
    seven_d_reset        INTEGER,
    opus_status          TEXT,
    opus_utilization     REAL,
    opus_reset           INTEGER,
    representative_claim TEXT,
    overage_status       TEXT,
    fallback_percentage  REAL,
    retry_after          INTEGER,
    extra                TEXT,
    request_id           TEXT
);
CREATE INDEX IF NOT EXISTS ratelimit_ts ON ratelimit(ts);

CREATE TABLE IF NOT EXISTS requests (
    id                    INTEGER PRIMARY KEY,
    ts                    INTEGER NOT NULL,
    path                  TEXT,
    http_status           INTEGER,
    request_id            TEXT UNIQUE,
    model                 TEXT,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_creation_tokens INTEGER,
    cache_read_tokens     INTEGER,
    stop_reason           TEXT,
    duration_ms           INTEGER
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
"""

# Response header -> ratelimit column. Anything matching KEEP that is not in
# here is preserved verbatim in the `extra` JSON blob, so a new header Anthropic
# starts sending is captured even before this map knows about it.
HEADER_COLUMNS = {
    "anthropic-ratelimit-unified-status": ("status", str),
    "anthropic-ratelimit-unified-5h-status": ("five_h_status", str),
    "anthropic-ratelimit-unified-5h-utilization": ("five_h_utilization", float),
    "anthropic-ratelimit-unified-5h-reset": ("five_h_reset", int),
    "anthropic-ratelimit-unified-7d-status": ("seven_d_status", str),
    "anthropic-ratelimit-unified-7d-utilization": ("seven_d_utilization", float),
    "anthropic-ratelimit-unified-7d-reset": ("seven_d_reset", int),
    "anthropic-ratelimit-unified-7d-opus-status": ("opus_status", str),
    "anthropic-ratelimit-unified-7d-opus-utilization": ("opus_utilization", float),
    "anthropic-ratelimit-unified-7d-opus-reset": ("opus_reset", int),
    "anthropic-ratelimit-unified-representative-claim": ("representative_claim", str),
    "anthropic-ratelimit-unified-overage-status": ("overage_status", str),
    "anthropic-ratelimit-unified-fallback-percentage": ("fallback_percentage", float),
    "retry-after": ("retry_after", int),
}
RATELIMIT_COLUMNS = (["ts"] + [c for c, _ in HEADER_COLUMNS.values()]
                     + ["extra", "request_id"])
# Columns added after the first release, applied to older databases on open.
MIGRATIONS = {"ratelimit": {"request_id": "TEXT"}}

# Columns whose change makes a sample worth storing. Everything else (a reset
# timestamp ticking over, say) rides along on the next real change.
DEDUP_ON = ("status", "five_h_status", "five_h_utilization", "five_h_reset",
            "seven_d_status", "seven_d_utilization", "seven_d_reset",
            "opus_status", "opus_utilization", "overage_status", "retry_after")


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    else:
        conn = sqlite3.connect(path, timeout=30)
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for column, kind in columns.items():
                if column not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        conn.commit()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


class Recorder:
    """Bounded write queue drained by one background thread.

    Callers never block and never see an exception: a full queue or a broken
    database costs a dropped sample, nothing more.
    """

    def __init__(self, db_path: Path, maxsize: int = 10_000):
        self.db_path = db_path
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.dropped = 0
        self.written = 0
        self.last_error: str | None = None
        self._last_signal: tuple | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def ratelimit(self, headers: dict[str, str], request_id: str | None = None):
        row: dict = {c: None for c in RATELIMIT_COLUMNS}
        row["request_id"] = request_id
        extra = {}
        for name, value in headers.items():
            key = name.lower()
            mapped = HEADER_COLUMNS.get(key)
            if mapped is None:
                extra[key] = value
                continue
            column, cast = mapped
            try:
                row[column] = cast(value)
            except (TypeError, ValueError):
                extra[key] = value
        if not any(row[c] is not None for c in RATELIMIT_COLUMNS
                   if c not in ("ts", "request_id")):
            return  # nothing but noise
        signal = tuple(row[c] for c in DEDUP_ON)
        if signal == self._last_signal:
            return
        self._last_signal = signal
        row["ts"] = int(time.time())
        row["extra"] = json.dumps(extra, sort_keys=True) if extra else None
        self._put(("ratelimit", row))

    def request(self, row: dict):
        self._put(("requests", row))

    def _put(self, item):
        try:
            self.q.put_nowait(item)
        except queue.Full:
            self.dropped += 1

    def _run(self):
        conn = None
        while True:
            table, row = self.q.get()
            try:
                if conn is None:
                    conn = connect(self.db_path)
                cols = ", ".join(row)
                marks = ", ".join("?" * len(row))
                verb = "INSERT OR IGNORE" if table == "requests" else "INSERT"
                conn.execute(f"{verb} INTO {table} ({cols}) VALUES ({marks})",
                             list(row.values()))
                conn.commit()
                self.written += 1
            except Exception as exc:  # never propagate into the request path
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.dropped += 1
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None


# --------------------------------------------------------------------------- #
# Usage sniffing
# --------------------------------------------------------------------------- #

class UsageSniffer:
    """Pulls model + token counts out of a response without holding it in RAM.

    Streaming responses are SSE: `message_start` carries the model and input
    token counts, `message_delta` the final output count and stop reason. Both
    are single lines, so we scan line-wise and keep only what we need.
    Non-streaming JSON bodies are buffered up to `cap` and parsed at the end.
    """

    def __init__(self, cap: int = 1 << 20):
        self.cap = cap
        self.model: str | None = None
        self.input_tokens = self.output_tokens = 0
        self.cache_creation = self.cache_read = 0
        self.stop_reason: str | None = None
        self._pending = bytearray()
        self._whole = bytearray()
        self._sse = False

    def feed(self, chunk: bytes):
        if len(self._whole) < self.cap:
            self._whole += chunk
        self._pending += chunk
        if b"\n" not in chunk:
            # Guard against a body with no newlines growing the line buffer.
            if len(self._pending) > self.cap:
                del self._pending[:-4096]
            return
        *lines, rest = self._pending.split(b"\n")
        self._pending = bytearray(rest)
        for line in lines:
            if line.startswith(b"data: ") and b'"usage"' in line:
                self._sse = True
                self._absorb(line[6:])

    def finish(self):
        if self._sse or len(self._whole) >= self.cap:
            return
        self._absorb(bytes(self._whole))

    def _absorb(self, blob: bytes):
        try:
            payload = json.loads(blob)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
        model = message.get("model")
        if isinstance(model, str) and model not in ("", "<synthetic>"):
            self.model = model
        usage = payload.get("usage") or message.get("usage")
        if isinstance(usage, dict):
            # message_start reports input side; message_delta the final output.
            self.input_tokens = max(self.input_tokens, usage.get("input_tokens") or 0)
            self.output_tokens = max(self.output_tokens, usage.get("output_tokens") or 0)
            self.cache_creation = max(self.cache_creation,
                                      usage.get("cache_creation_input_tokens") or 0)
            self.cache_read = max(self.cache_read,
                                  usage.get("cache_read_input_tokens") or 0)
        stop = (payload.get("delta") or {}).get("stop_reason") if isinstance(
            payload.get("delta"), dict) else payload.get("stop_reason")
        if isinstance(stop, str):
            self.stop_reason = stop


# --------------------------------------------------------------------------- #
# Resolution that ignores /etc/hosts
# --------------------------------------------------------------------------- #

class Resolver:
    """Resolves the upstream host from DNS, bypassing /etc/hosts.

    The intercept works by pointing api.anthropic.com at 127.0.0.1, so the
    system resolver would send the proxy straight back to itself. We ask the
    configured nameservers over UDP instead and cache what they say.

    Failing that, `getaddrinfo` with loopback answers filtered out is still
    better than nothing: it picks up a DNS search path we don't implement, and
    it can only ever return the real host.
    """

    MIN_TTL, MAX_TTL, TIMEOUT = 60, 300, 3

    def __init__(self, servers: list[str] | None = None):
        self.servers = servers or self._system_servers()
        self.cache: dict[str, tuple[list[str], float]] = {}
        self.lock = threading.Lock()

    @staticmethod
    def _system_servers() -> list[str]:
        found = []
        try:
            for line in Path("/etc/resolv.conf").read_text().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    found.append(parts[1])
        except OSError:
            pass
        return found or ["1.1.1.1", "8.8.8.8"]

    def resolve(self, host: str) -> list[str]:
        try:
            ipaddress.ip_address(host)
            return [host]  # already a literal
        except ValueError:
            pass
        now = time.monotonic()
        with self.lock:
            cached = self.cache.get(host)
        if cached and cached[1] > now:
            return cached[0]
        addresses, ttl = self._query(host)
        if addresses:
            with self.lock:
                self.cache[host] = (addresses, now + ttl)
            return addresses
        if cached:
            return cached[0]  # stale beats nothing at all
        return self._fallback(host)

    def _query(self, host: str) -> tuple[list[str], int]:
        question = b"".join(bytes([len(part)]) + part
                            for part in host.encode("idna").split(b".")) + b"\0"
        for server in self.servers:
            ident = secrets.randbits(16)
            packet = (ident.to_bytes(2, "big") + b"\x01\x00\x00\x01" + b"\x00" * 6
                      + question + b"\x00\x01\x00\x01")
            family = socket.AF_INET6 if ":" in server else socket.AF_INET
            try:
                with socket.socket(family, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(self.TIMEOUT)
                    sock.sendto(packet, (server, 53))
                    deadline = time.monotonic() + self.TIMEOUT
                    while time.monotonic() < deadline:
                        reply = sock.recv(4096)
                        if len(reply) >= 12 and reply[:2] == packet[:2]:
                            addresses, ttl = self._parse(reply)
                            if addresses:
                                return addresses, ttl
                            break
            except Exception:
                continue
        return [], 0

    @classmethod
    def _parse(cls, reply: bytes) -> tuple[list[str], int]:
        questions = int.from_bytes(reply[4:6], "big")
        answers = int.from_bytes(reply[6:8], "big")
        pos = 12
        for _ in range(questions):
            pos = cls._skip_name(reply, pos) + 4
        addresses, ttl = [], cls.MAX_TTL
        for _ in range(answers):
            pos = cls._skip_name(reply, pos)
            if pos + 10 > len(reply):
                break
            rtype = int.from_bytes(reply[pos:pos + 2], "big")
            record_ttl = int.from_bytes(reply[pos + 4:pos + 8], "big")
            length = int.from_bytes(reply[pos + 8:pos + 10], "big")
            pos += 10
            # Type 1 is A. CNAME chains resolve in the same answer section, so
            # collecting every A record regardless of owner name is enough.
            if rtype == 1 and length == 4:
                addresses.append(".".join(str(b) for b in reply[pos:pos + 4]))
                ttl = min(ttl, record_ttl)
            pos += length
        return addresses, max(cls.MIN_TTL, min(cls.MAX_TTL, ttl))

    @staticmethod
    def _skip_name(buf: bytes, pos: int) -> int:
        while pos < len(buf):
            length = buf[pos]
            if length == 0:
                return pos + 1
            if length & 0xC0 == 0xC0:
                return pos + 2  # compression pointer, always terminal
            pos += length + 1
        return pos

    @staticmethod
    def _fallback(host: str) -> list[str]:
        try:
            infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            return []
        return [info[4][0] for info in infos if not is_loopback(info[4][0])]


def is_loopback(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    # A v4 client on the dual-stack listener arrives as ::ffff:127.0.0.1, which
    # is not is_loopback until it's unmapped. Only IPv6Address has the attribute.
    return (getattr(parsed, "ipv4_mapped", None) or parsed).is_loopback


# --------------------------------------------------------------------------- #
# Certificates
# --------------------------------------------------------------------------- #

CA_CONFIG = f"""
[req]
distinguished_name = dn
prompt = no
[dn]
CN = {CA_SUBJECT}
O = claude-ratelimit-proxy
[v3_ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
"""

LEAF_CONFIG = f"""
[req]
distinguished_name = dn
prompt = no
[dn]
CN = {UPSTREAM_HOST}
[v3_leaf]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid, issuer
subjectAltName = DNS:{UPSTREAM_HOST}, DNS:localhost, IP:127.0.0.1, IP:::1
"""


def _openssl(*argv: str) -> subprocess.CompletedProcess:
    result = subprocess.run([OPENSSL, *argv], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"openssl {argv[0]}: "
                           f"{result.stderr.strip() or result.stdout.strip()}")
    return result


def _genkey(path: Path):
    _openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(path))
    path.chmod(0o600)


def cert_expiry(path: Path) -> datetime | None:
    """notAfter as an aware datetime, or None if unreadable."""
    try:
        out = _openssl("x509", "-noout", "-enddate", "-in", str(path)).stdout
        stamp = out.split("=", 1)[1].strip().removesuffix(" GMT")
        return datetime.strptime(stamp, "%b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _expiring(path: Path, within: int) -> bool:
    if not path.exists():
        return True
    return subprocess.run([OPENSSL, "x509", "-checkend", str(within), "-noout",
                           "-in", str(path)], capture_output=True).returncode != 0


def ensure_certs(force: bool = False) -> list[str]:
    """Create the CA and leaf if missing, renew the leaf before it lapses.

    Returns what it did. The CA is left alone unless forced: regenerating it
    would silently invalidate the keychain trust the user granted the old one.
    """
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    CERT_DIR.chmod(0o700)
    done = []

    if force or not (CA_CERT.exists() and CA_KEY.exists()):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "ca.cnf"
            config.write_text(CA_CONFIG)
            _genkey(CA_KEY)
            _openssl("req", "-x509", "-new", "-key", str(CA_KEY), "-sha256",
                     "-days", str(CA_DAYS), "-out", str(CA_CERT),
                     "-config", str(config), "-extensions", "v3_ca")
        done.append(f"created CA {CA_CERT} (valid {CA_DAYS} days)")
        force = True  # a new CA invalidates any existing leaf

    if force or _expiring(LEAF_CERT, RENEW_WITHIN) or not LEAF_KEY.exists():
        with tempfile.TemporaryDirectory() as tmp:
            config, csr = Path(tmp) / "leaf.cnf", Path(tmp) / "leaf.csr"
            config.write_text(LEAF_CONFIG)
            _genkey(LEAF_KEY)
            _openssl("req", "-new", "-key", str(LEAF_KEY), "-out", str(csr),
                     "-config", str(config))
            _openssl("x509", "-req", "-in", str(csr), "-CA", str(CA_CERT),
                     "-CAkey", str(CA_KEY), "-CAcreateserial",
                     "-CAserial", str(CERT_DIR / "ca.srl"), "-sha256",
                     "-days", str(LEAF_DAYS), "-out", str(LEAF_CERT),
                     "-extfile", str(config), "-extensions", "v3_leaf")
        done.append(f"issued leaf for {UPSTREAM_HOST} (valid {LEAF_DAYS} days)")
    return done


def ca_trusted() -> bool:
    """Whether our CA is sitting in the system keychain."""
    return subprocess.run(["security", "find-certificate", "-c", CA_SUBJECT,
                           SYSTEM_KEYCHAIN], capture_output=True).returncode == 0


def stale_clients() -> list[str]:
    """Running Claude processes that predate the CA, and so cannot trust it.

    A process reads its trust store once, at startup. Anything started before
    the CA existed will reject the proxy's certificate no matter what the
    keychain says, and the symptom is an SSL verification error that looks
    exactly like a broken intercept. The fix is a restart, never more config.
    """
    try:
        ca_age = time.time() - CA_CERT.stat().st_mtime
    except OSError:
        return []
    try:
        listing = subprocess.run(["ps", "-eo", "pid,etime,comm"],
                                 capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    head, *rows = listing.splitlines()
    if "ELAPSED" not in head:
        return []  # ps dropped the column rather than failing; don't guess
    seen: dict[str, int] = {}
    for line in rows:
        parts = line.split(None, 2)
        if len(parts) < 3 or "claude" not in parts[2].lower():
            continue
        age = _elapsed_seconds(parts[1])
        if age is None or age < ca_age:
            continue
        name = Path(parts[2].strip()).name
        seen[name] = seen.get(name, 0) + 1
    return [f"{name} (x{n})" if n > 1 else name for name, n in sorted(seen.items())]


def _elapsed_seconds(value: str) -> int | None:
    """Parse BSD ps ELAPSED, which is [[DD-]HH:]MM:SS."""
    days, _, clock = value.rpartition("-")
    fields = clock.split(":")
    if not 2 <= len(fields) <= 3 or not all(f.isdigit() for f in fields):
        return None
    seconds = 0
    for field in fields:
        seconds = seconds * 60 + int(field)
    if days:
        if not days.isdigit():
            return None
        seconds += int(days) * 86400
    return seconds


def hosts_entries() -> list[str]:
    """Loopback addresses /etc/hosts currently maps the upstream name to."""
    found = []
    try:
        for line in HOSTS_PATH.read_text().splitlines():
            line = line.split("#", 1)[0].split()
            if len(line) >= 2 and UPSTREAM_HOST in line[1:]:
                found.append(line[0])
    except OSError:
        pass
    return found


# --------------------------------------------------------------------------- #
# Upstream connection pool
# --------------------------------------------------------------------------- #

class Upstream:
    """Small LIFO pool of TLS connections, so we don't pay a handshake a turn."""

    IDLE_TTL = 50  # seconds; well under any sane server-side idle timeout

    # A pooled connection can die between the IDLE_TTL check and reuse - the
    # far end (or a NAT/router in between) can close or silently drop it at
    # any moment, and that's not always a clean FIN we can see coming. Two
    # independent nets for that race:
    #  - _dropped() catches a clean close: an idle socket is only readable if
    #    the peer already sent something unsolicited, which for a connection
    #    we asked nothing of means EOF.
    #  - a short read timeout on a *reused* connection's first response byte
    #    catches a silent drop (no FIN ever arrives): rather than hang on
    #    getresponse() for up to `timeout` seconds with nothing to show for
    #    it, fail fast and let send()'s existing retry-on-fresh-connection
    #    path recover invisibly. Fresh connections keep the full timeout,
    #    since legitimate first-byte latency (large prompts, extended
    #    thinking) can be much longer than this.
    #
    #    That same latency argument applies to a *just-reused* connection too:
    #    one handed back to the pool moments ago and pulled straight back out
    #    hasn't been idle long enough for a NAT/LB to have silently reaped it
    #    - a slow getresponse() on it is real upstream latency, not a dead
    #    socket, so firing the fast timeout there only manufactures a
    #    duplicate request. Only apply it once idle time makes a silent drop
    #    plausible.
    STALE_READ_TIMEOUT = 15
    STALE_CHECK_MIN_IDLE = 30

    def __init__(self, host: str, timeout: int, resolver: Resolver, size: int = 8):
        self.host, self.timeout, self.size = host, timeout, size
        self.resolver = resolver
        self.ctx = ssl.create_default_context()
        self.pool: collections.deque = collections.deque()
        self.lock = threading.Lock()
        self.stats = {"pool_dropped": 0, "pool_stale_timeout": 0}

    @staticmethod
    def _dropped(conn) -> bool:
        sock = getattr(conn, "sock", None)
        if sock is None:
            return False
        try:
            return bool(select.select([sock], [], [], 0)[0])
        except (OSError, ValueError):
            return True

    def _dial(self, address, timeout, source_address):
        """Stand-in for socket.create_connection that skips /etc/hosts.

        http.client calls this with (self.host, port), so `conn.host` stays the
        real name and SNI plus the Host header are correct; only the address we
        actually connect to comes from our own resolver.
        """
        host, port = address
        addresses = self.resolver.resolve(host)
        if not addresses:
            raise OSError(f"cannot resolve {host}")
        error: Exception | None = None
        for candidate in addresses:
            if is_loopback(candidate):
                continue  # that's us; the hosts entry is not for our benefit
            try:
                return socket.create_connection((candidate, port), timeout,
                                                source_address)
            except OSError as exc:
                error = exc
        raise error or OSError(f"no usable address for {host}")

    def _take(self) -> tuple[http.client.HTTPSConnection, float]:
        """Returns (conn, idle_age) — idle_age is 0.0 for a freshly dialed conn."""
        now = time.monotonic()
        with self.lock:
            while self.pool:
                conn, idle_since = self.pool.pop()
                if now - idle_since >= self.IDLE_TTL:
                    _close(conn)
                    continue
                if self._dropped(conn):
                    self.stats["pool_dropped"] += 1
                    _close(conn)
                    continue
                return conn, now - idle_since
        conn = http.client.HTTPSConnection(self.host, timeout=self.timeout,
                                           context=self.ctx)
        conn._create_connection = self._dial  # type: ignore[method-assign]
        return conn, 0.0

    def give_back(self, conn):
        with self.lock:
            if len(self.pool) < self.size:
                self.pool.append((conn, time.monotonic()))
                return
        _close(conn)

    def send(self, method: str, path: str, headers: dict, body: bytes | None):
        """Returns (conn, response). Retries once on a dead pooled connection.

        A connection error before any response byte means upstream never
        processed the request, so replaying it is safe.
        """
        error = None
        for attempt in range(2):
            conn, idle_age = self._take()
            reused = conn.sock is not None
            fast_check = reused and idle_age >= self.STALE_CHECK_MIN_IDLE
            stalled_at = time.monotonic()
            try:
                conn.request(method, path, body=body, headers=headers)
                if fast_check:
                    conn.sock.settimeout(self.STALE_READ_TIMEOUT)
                try:
                    response = conn.getresponse()
                except (TimeoutError, OSError) as exc:
                    if fast_check:
                        self.stats["pool_stale_timeout"] += 1
                        self._log_stale(method, path, body, idle_age,
                                        time.monotonic() - stalled_at, attempt, exc)
                    raise
                if fast_check:
                    conn.sock.settimeout(self.timeout)
                return conn, response
            except Exception as exc:
                _close(conn)
                error = exc
        raise error  # type: ignore[misc]

    @staticmethod
    def _is_streaming(body: bytes | None) -> str:
        if not body:
            return "?"
        try:
            payload = json.loads(body)
            return str(bool(payload.get("stream"))) if isinstance(payload, dict) else "?"
        except Exception:
            return "?"

    def _log_stale(self, method: str, path: str, body: bytes | None,
                    idle_age: float, waited: float, attempt: int, exc: Exception):
        """Diagnostic for the reused-connection fast-fail path.

        idle_age is how long the connection sat in the pool before reuse (helps
        tell "died while idle" apart from "died mid-request"); waited is how
        long we actually blocked on this attempt before giving up. attempt==0
        means send() will retry on a fresh connection next; attempt==1 means
        that retry also failed and the caller is about to surface a 502 - if
        that happens on a live, slow-to-respond upstream (streaming=False,
        waited close to STALE_READ_TIMEOUT), this path likely just fired a
        false positive and duplicated the request.
        """
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        outcome = "retrying fresh" if attempt == 0 else "retry ALSO failed, giving up"
        sys.stderr.write(
            f"[{ts}] stale-timeout: {method} {path}  "
            f"idle_age={idle_age:.1f}s waited={waited:.1f}s "
            f"streaming={self._is_streaming(body)} "
            f"error={type(exc).__name__}({exc}) -> {outcome}\n")
        sys.stderr.flush()


def _close(conn):
    try:
        conn.close()
    except Exception:
        pass


def open_fds() -> int:
    """Descriptors this process holds open. The listing costs one itself."""
    return len(os.listdir("/dev/fd")) - 1


# --------------------------------------------------------------------------- #
# Request rewriting
# --------------------------------------------------------------------------- #

# Claude Code sends bare "claude-sonnet-5" for `/model sonnet` and for every
# subagent spawn, which lands on the 200K window - only the interactive
# "Sonnet (1M)" menu entry sends "claude-sonnet-5[1m]". Rewrite the former to
# the latter so every route (including subagents) gets the long window.
LONG_CONTEXT_MODELS = {"claude-sonnet-5": "claude-sonnet-5[1m]"}


def _force_long_context(body: bytes) -> bytes:
    try:
        payload = json.loads(body)
        replacement = LONG_CONTEXT_MODELS.get(payload.get("model")) \
            if isinstance(payload, dict) else None
        if replacement:
            payload["model"] = replacement
            return json.dumps(payload).encode()
    except Exception:
        pass  # fail open: forward the original body unchanged
    return body


# --------------------------------------------------------------------------- #
# Proxy
# --------------------------------------------------------------------------- #

class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 900

    recorder: Recorder
    upstream: Upstream
    started_at: float
    counters: dict
    verbose: bool

    def log_message(self, *args):  # launchd logs are for errors only
        pass

    def log_error(self, fmt, *args):
        if self.verbose:
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    # -- helpers ----------------------------------------------------------- #

    def _health(self):
        body = json.dumps({
            "ok": True,
            "pid": os.getpid(),
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "upstream": UPSTREAM_HOST,
            "db": str(self.recorder.db_path),
            "requests": self.counters["requests"],
            "errors": self.counters["errors"],
            "pool_dropped": self.upstream.stats["pool_dropped"],
            "pool_stale_timeout": self.upstream.stats["pool_stale_timeout"],
            "rows_written": self.recorder.written,
            "rows_dropped": self.recorder.dropped,
            "last_write_error": self.recorder.last_error,
            "fds": open_fds(),
            "fd_limit": resource.getrlimit(resource.RLIMIT_NOFILE)[0],
        }, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self):
        if self.path.startswith("/__proxy/"):
            self._health() if self.path == "/__proxy/health" else self.send_error(404)
            return

        started = time.monotonic()
        self.counters["requests"] += 1

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        # if body and self.path.split("?")[0] == "/v1/messages":
        #     body = _force_long_context(body)

        try:
            conn, response = self.upstream.send(self.command, self.path, headers, body)
        except Exception as exc:
            self.counters["errors"] += 1
            self.log_error("upstream %s: %s", self.path, exc)
            self.send_error(502, "upstream unreachable")
            return

        try:
            self._relay(conn, response, started)
        finally:
            pass

    def _relay(self, conn, response, started: float):
        content_length = response.getheader("Content-Length")
        chunked = content_length is None

        # send_response_only, not send_response: we relay upstream's Date and
        # Server rather than stacking a second copy of each on top of them.
        self.send_response_only(response.status)
        seen = set()
        for name, value in response.getheaders():
            key = name.lower()
            if key in HOP or (chunked and key == "content-length"):
                continue
            seen.add(key)
            self.send_header(name, value)
        if "date" not in seen:
            self.send_header("Date", self.date_time_string())
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        sniffer = UsageSniffer()
        reusable = True
        try:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                if chunked:
                    self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
                try:
                    sniffer.feed(chunk)
                except Exception:
                    pass  # sniffing is best-effort, the stream is what matters
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except Exception as exc:
            # Client hung up or upstream died mid-stream. Log and move on; the
            # connection is no longer safe to reuse.
            reusable = False
            self.close_connection = True
            self.log_error("stream %s: %s", self.path, exc)

        if reusable and response.isclosed():
            self.upstream.give_back(conn)
        else:
            _close(conn)

        try:
            self._record(response, sniffer, started)
        except Exception as exc:
            self.recorder.last_error = f"record: {type(exc).__name__}: {exc}"

    def _record(self, response, sniffer: UsageSniffer, started: float):
        signal = {name: value for name, value in response.getheaders()
                  if KEEP.search(name) and not SECRET.search(name)}
        request_id = response.getheader("request-id")
        if signal:
            self.recorder.ratelimit(signal, request_id)

        sniffer.finish()
        if sniffer.model or request_id:
            self.recorder.request({
                "ts": int(time.time()),
                "path": self.path.split("?")[0],
                "http_status": response.status,
                "request_id": request_id,
                "model": sniffer.model,
                "input_tokens": sniffer.input_tokens,
                "output_tokens": sniffer.output_tokens,
                "cache_creation_tokens": sniffer.cache_creation,
                "cache_read_tokens": sniffer.cache_read,
                "stop_reason": sniffer.stop_reason,
                "duration_ms": int((time.monotonic() - started) * 1000),
            })

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _forward


class _Handshake(Exception):
    """A client that never got as far as speaking HTTP. Not worth a traceback."""


class TLSProxy(Proxy):
    """Same proxy, reached by impersonating the upstream name over TLS."""

    def setup(self):
        # Handshake here rather than at accept(), so a slow or dead client
        # stalls its own worker thread instead of the whole accept loop.
        try:
            self.request = self.server.tls.wrap_socket(self.request, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            self.log_error("handshake %s: %s", self.client_address[0], exc)
            raise _Handshake(exc) from None
        super().setup()

    def finish(self):
        # wrap_socket moved the descriptor into the SSL socket and left the
        # original object empty. socketserver only ever closes the original,
        # so this one is ours: left to the GC, a traceback that keeps the
        # handler alive keeps the descriptor with it, and the process bleeds
        # to its fd limit one hung-up client at a time.
        try:
            super().finish()
        finally:
            try:
                self.request.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            _close(self.request)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def verify_request(self, request, client_address):
        # macOS accept() hands back a zero-length peer address when the client
        # resets between SYN and accept, and Python renders that as None. It
        # arrives on the accept path, where an exception would kill the whole
        # listener, so this has to be total.
        return client_address is not None

    def handle_error(self, request, client_address):
        """A dropped connection is weather, not an error.

        The default prints a full traceback per reset, which buried anything
        real in the launchd log.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (_Handshake, ConnectionError, TimeoutError, BrokenPipeError)):
            return
        if Proxy.verbose:
            super().handle_error(request, client_address)
        else:
            sys.stderr.write(f"error from {client_address[0]}: "
                             f"{type(exc).__name__}: {exc}\n")


class TLSServer(Server):
    """Dual-stack listener on the privileged port, loopback clients only.

    macOS only demands privilege to bind a low port to a *specific* address, so
    the wildcard bind gets :443 without root. The flip side is that the socket
    is reachable from the network, hence the peer check below — it runs on the
    accept path, before the handshake, so a scanner gets a closed connection
    and never sees a certificate.
    """

    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

    def verify_request(self, request, client_address):
        return (super().verify_request(request, client_address)
                and is_loopback(client_address[0]))


def serve(args):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for line in ensure_certs():
        print(line, file=sys.stderr, flush=True)

    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    # No h2: the relay speaks HTTP/1.1, and a browser offered h2 would take it.
    tls.set_alpn_protocols(["http/1.1"])
    tls.load_cert_chain(LEAF_CERT, LEAF_KEY)

    recorder = Recorder(args.db)
    recorder.start()

    # Warm the cache before the hosts entry can matter, and say out loud where
    # upstream actually lives: a wrong answer here is the one failure mode that
    # looks like Anthropic being down.
    resolver = Resolver()
    found = resolver.resolve(UPSTREAM_HOST)
    print(f"upstream {UPSTREAM_HOST} -> {', '.join(found) or 'UNRESOLVED'} "
          f"(via {', '.join(resolver.servers)})", file=sys.stderr, flush=True)

    Proxy.recorder = recorder
    Proxy.upstream = Upstream(UPSTREAM_HOST, timeout=args.timeout, resolver=resolver)
    Proxy.started_at = time.monotonic()
    Proxy.counters = {"requests": 0, "errors": 0}
    Proxy.verbose = args.verbose

    plain = Server(("127.0.0.1", args.port), Proxy)
    try:
        secure = TLSServer(("::", args.tls_port), TLSProxy)
    except OSError as exc:
        plain.server_close()
        print(f"cannot listen on :{args.tls_port}: {exc}", file=sys.stderr)
        return 1
    secure.tls = tls

    expiry = cert_expiry(LEAF_CERT)
    leaf = f", leaf to {expiry:%Y-%m-%d}" if expiry else ""
    print(f"proxy -> https://{UPSTREAM_HOST}  on [::]:{args.tls_port} (tls{leaf}) "
          f"and 127.0.0.1:{args.port} (plain)  db={args.db}",
          file=sys.stderr, flush=True)

    # The accept loop must not be able to die quietly. With the hosts entry in
    # place a stopped TLS listener takes every api.anthropic.com client on the
    # machine down with it, and launchd cannot see the difference because the
    # process is still alive.
    def guard(server):
        while True:
            try:
                server.serve_forever()
                return  # clean shutdown
            except Exception as exc:
                sys.stderr.write(f"tls accept loop died: "
                                 f"{type(exc).__name__}: {exc}; restarting\n")
                sys.stderr.flush()
                time.sleep(0.5)

    threading.Thread(target=guard, args=(secure,), daemon=True).start()
    try:
        plain.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        secure.shutdown()
        plain.server_close()
        secure.server_close()
    return 0


# --------------------------------------------------------------------------- #
# launchd
# --------------------------------------------------------------------------- #

def plist_body(port: int, tls_port: int, db: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/opt/homebrew/bin/uv", "run", str(SELF), "serve",
                             "--port", str(port), "--tls-port", str(tls_port),
                             "--db", str(db)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "StandardOutPath": str(DATA_DIR / "launchd.out.log"),
        "StandardErrorPath": str(DATA_DIR / "launchd.err.log"),
    }


def _launchctl(*argv) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *argv], capture_output=True, text=True)


def install(args):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for line in ensure_certs():
        print(line)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_bytes(plistlib.dumps(
        plist_body(args.port, args.tls_port, args.db)))
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}")  # ignore "not loaded"
    result = _launchctl("bootstrap", domain, str(PLIST_PATH))
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        return 1
    print(f"installed {PLIST_PATH}")
    for _ in range(20):
        time.sleep(0.25)
        if health(args.port) is not None:
            break
    rc = status(args)
    if not (ca_trusted() and hosts_entries()):
        print()
        trust(args)
    return rc


def uninstall(args):
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        print(f"removed {PLIST_PATH}")
    else:
        print("no launchd agent installed")
    print("\nThe intercept outlives the agent; back it out too, or every\n"
          f"{UPSTREAM_HOST} request on this machine fails:\n\n"
          f"  sudo sed -i '' '/{UPSTREAM_HOST}/d' /etc/hosts\n"
          "  sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder\n"
          f"  sudo security delete-certificate -c '{CA_SUBJECT}' {SYSTEM_KEYCHAIN}\n")
    return 0


def cert(args):
    changed = ensure_certs(force=args.force)
    for line in changed:
        print(line)
    if not changed:
        print("certificates are current")
    expiry = cert_expiry(LEAF_CERT)
    print(f"  CA   {CA_CERT}  (trusted: {'yes' if ca_trusted() else 'NO'})")
    print(f"  leaf {LEAF_CERT}"
          + (f"  (expires {expiry:%a %d %b %Y})" if expiry else ""))
    if changed and ca_trusted() and any("created CA" in line for line in changed):
        print("\nThe old CA is still in the keychain and no longer signs anything.")
        print(f"  sudo security delete-certificate -c '{CA_SUBJECT}' {SYSTEM_KEYCHAIN}")
    return 0


def trust(args):
    """Print what still needs root. Nothing here runs itself.

    The order is the whole point. Redirecting the name before every client
    trusts the CA breaks each of them until it restarts.
    """
    print("Three steps, in this order. Step 2 is the one that bites.\n")

    if ca_trusted():
        print("  [done] 1. CA is trusted in the system keychain\n")
    else:
        print("  # 1. trust the CA for every TLS client on this machine")
        print(f"  sudo security add-trusted-cert -d -r trustRoot \\\n"
              f"    -k {SYSTEM_KEYCHAIN} {CA_CERT}\n")

    stale = stale_clients()
    print("  # 2. restart every client that predates the CA. A process reads")
    print("  #    its trust store once, at startup, so anything already running")
    print("  #    rejects the certificate however the keychain is configured.")
    if stale:
        print(f"  #    still running from before the CA: {', '.join(stale)}")
        print("  #    quit Claude Desktop, exit other Claude Code sessions,")
        print("  #    restart Chrome, then re-run this command to confirm.\n")
    else:
        print("  #    nothing running predates the CA right now.\n")

    if hosts_entries():
        print(f"  [done] 3. /etc/hosts sends {UPSTREAM_HOST} to "
              f"{', '.join(hosts_entries())}")
    else:
        print(f"  # 3. send {UPSTREAM_HOST} to the proxy — last, and only once")
        print("  #    step 2 is clean, or you take every client down with it")
        print(f"  printf '\\n{HOSTS_MARK}\\n127.0.0.1 {UPSTREAM_HOST}\\n"
              f"::1 {UPSTREAM_HOST}\\n' | sudo tee -a /etc/hosts")
        print("  sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder")

    if os.environ.get("ANTHROPIC_BASE_URL"):
        print("\nAlso drop ANTHROPIC_BASE_URL from ~/.claude/settings.json and your\n"
              "shell profile: the whole point is that the base URL stays default,\n"
              "which is what Remote Control checks for.")
    return 0


def health(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/__proxy/health", timeout=3) as fh:
            return json.load(fh)
    except Exception:
        return None


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _local(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M")


def _since(days: float) -> int:
    return int(time.time() - days * 86400)


def status(args):
    listed = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    if listed.returncode == 0:
        state = re.search(r"state = (\S+)", listed.stdout)
        pid = re.search(r"pid = (\d+)", listed.stdout)
        print(f"launchd : {state.group(1) if state else '?'}"
              f"{'  pid ' + pid.group(1) if pid else ''}")
    else:
        print("launchd : not installed")

    info = health(args.port)
    if info is None:
        print(f"health  : NOT RESPONDING on 127.0.0.1:{args.port}")
    else:
        print(f"health  : ok, up {timedelta(seconds=int(info['uptime_s']))}, "
              f"{info['requests']} requests, {info['errors']} errors, "
              f"{info['rows_written']} rows written, {info['rows_dropped']} dropped")
        pool_dropped = info.get("pool_dropped", 0)
        pool_stale = info.get("pool_stale_timeout", 0)
        print(f"pool    : {pool_dropped} dead pooled conns dropped, "
              f"{pool_stale} stale-timeout recoveries")
        fds, limit = info.get("fds"), info.get("fd_limit")
        if fds is not None and limit:
            warn = "  <- near the limit, restart it" if fds > limit * 0.8 else ""
            print(f"fds     : {fds} of {limit} open{warn}")
        if info.get("last_write_error"):
            print(f"          last write error: {info['last_write_error']}")

    entries = hosts_entries()
    print(f"hosts   : {UPSTREAM_HOST} -> {', '.join(entries) if entries else 'NOT intercepted'}")

    expiry = cert_expiry(LEAF_CERT)
    if expiry is None:
        print("cert    : no leaf certificate yet (run: proxy.py cert)")
    else:
        left = (expiry - datetime.now(timezone.utc)).days
        print(f"cert    : leaf expires {expiry:%a %d %b %Y} ({left}d), "
              f"CA {'trusted' if ca_trusted() else 'NOT TRUSTED'}")
    stale = stale_clients()
    if stale:
        print(f"stale   : started before the CA, so will reject it until "
              f"restarted: {', '.join(stale)}")

    listening = _listening(args.tls_port)
    print(f"tls     : {'listening' if listening else 'NOT LISTENING'} on :{args.tls_port}")

    env = os.environ.get("ANTHROPIC_BASE_URL")
    if env:
        print(f"env     : ANTHROPIC_BASE_URL={env}  <- unset this, it disables "
              f"Remote Control")

    if not args.db.exists():
        print(f"db      : {args.db} does not exist yet")
        return 0
    with connect(args.db, readonly=True) as conn:
        row = conn.execute("SELECT * FROM ratelimit ORDER BY ts DESC LIMIT 1").fetchone()
        samples = conn.execute("SELECT COUNT(*) c FROM ratelimit").fetchone()["c"]
        reqs = conn.execute("SELECT COUNT(*) c FROM requests").fetchone()["c"]
        shifts, _ = _shifts(conn, PROMO_WEEKLY, since=_since(14))
    print(f"db      : {args.db}  ({samples} signal rows, {reqs} requests)")
    if row is None:
        print("signal  : nothing recorded yet")
        return 0
    print(f"\nlatest signal ({_local(row['ts'])}), status {row['status']}:")
    for label, util, reset in (("5h ", row["five_h_utilization"], row["five_h_reset"]),
                               ("7d ", row["seven_d_utilization"], row["seven_d_reset"]),
                               ("7d opus", row["opus_utilization"], row["opus_reset"])):
        if util is None:
            continue
        bar = "#" * int(round(util * 40)) + "." * (40 - int(round(util * 40)))
        when = f"resets {_local(reset)}" if reset else ""
        print(f"  {label:<8} {util * 100:5.1f}%  [{bar}]  {when}")
    for _, t1, u0, u1, _ in shifts:
        print(f"\nDENOMINATOR SHIFT {_local(t1)}: 7d jumped {u0 * 100:.0f}% -> "
              f"{u1 * 100:.0f}% on traffic that cannot explain it; the weekly "
              f"cap moved x{u0 / u1:.2f}.  See `proxy.py calibrate`.")
    return 0


def history(args):
    if not args.db.exists():
        print("no database yet", file=sys.stderr)
        return 1
    with connect(args.db, readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM ratelimit WHERE ts >= ? ORDER BY ts", (_since(args.days),)
        ).fetchall()
    if not rows:
        print("no samples in that window")
        return 0
    print(f"{'when':<18} {'5h':>7} {'7d':>7} {'opus':>7}  status")
    for row in rows:
        def pct(value):
            return f"{value * 100:6.1f}%" if value is not None else "      -"
        flags = row["status"] or ""
        if row["retry_after"]:
            flags += f" retry-after={row['retry_after']}"
        print(f"{_local(row['ts']):<18} {pct(row['five_h_utilization'])} "
              f"{pct(row['seven_d_utilization'])} {pct(row['opus_utilization'])}  {flags}")
    return 0


def usage(args):
    if not args.db.exists():
        print("no database yet", file=sys.stderr)
        return 1
    with connect(args.db, readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM requests WHERE ts >= ? AND model IS NOT NULL",
            (_since(args.days),)).fetchall()
    if not rows:
        print("no requests in that window")
        return 0
    per: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0, 0, 0, 0, 0])
    for row in rows:
        agg = per[row["model"]]
        cost = credits(row["model"], row["input_tokens"], row["output_tokens"],
                       row["cache_creation_tokens"])
        agg[0] += 1
        agg[1] += cost
        agg[2] += row["input_tokens"]
        agg[3] += row["cache_creation_tokens"]
        agg[4] += row["output_tokens"]
        agg[5] += row["cache_read_tokens"]
    span = f"{_local(min(r['ts'] for r in rows))} -> {_local(max(r['ts'] for r in rows))}"
    print(f"{len(rows)} requests   {span}\n")
    for model, agg in sorted(per.items(), key=lambda kv: -kv[1][1]):
        unpriced = "" if model in PRICE else "  (unpriced)"
        print(f"  {model:<26} {agg[1]:>12,} cr  n {agg[0]:>5}  in {agg[2]:>10,}  "
              f"cw {agg[3]:>11,}  out {agg[4]:>9,}  cr-read {agg[5]:>12,}{unpriced}")
    print(f"\n  TOTAL: {sum(a[1] for a in per.values()):,} credits")
    return 0


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #

# The plan meters report utilisation, never the denominator, so a change to the
# weekly allowance is invisible in the number itself -- it shows up only as a
# jump no recorded traffic can account for. Anthropic ran a +50% weekly
# promotion through Sep 2026 and replaced it with a permanent +25%, so this
# does move. Nominal figure from she-llac.com/claude-limits (Max 20x).
NOMINAL_WEEKLY = 83_333_333
PROMO_WEEKLY = int(NOMINAL_WEEKLY * 1.5)
# The proxy only sees one machine's traffic; measured against the headers it
# accounts for roughly half to three quarters of what the account is charged.
# Detection assumes the pessimistic end so quiet windows do not false-positive.
VISIBLE_SHARE = 0.5


def _oi(row) -> float | None:
    """7d_oi ("overage included") utilisation, which rides in the extra blob.

    A second weekly meter running the same numerator over a smaller
    denominator: it reads ~1.2x the plain 7d figure, and is only emitted when
    representative_claim says it is the one being enforced.
    """
    try:
        extra = json.loads(row["extra"] or "{}")
    except ValueError:
        return None
    value = extra.get("anthropic-ratelimit-unified-7d_oi-utilization")
    return float(value) if value else None


def _credits_between(conn, t0: int, t1: int) -> int:
    rows = conn.execute(
        "SELECT model, input_tokens i, output_tokens o, cache_creation_tokens c "
        "FROM requests WHERE model IS NOT NULL AND model != '' "
        "AND ts > ? AND ts <= ?", (t0, t1))
    return sum(credits(r["model"], r["i"], r["o"], r["c"]) for r in rows)


def _shifts(conn, cap: float, since: int = 0):
    """Utilisation jumps too large for the traffic recorded between them.

    Both readings are scaled by the same unrecorded-traffic factor, so their
    ratio reads the denominator cleanly even though the proxy sees only part of
    the account's usage.
    """
    rows = conn.execute(
        "SELECT ts, seven_d_utilization u, seven_d_reset r FROM ratelimit "
        "WHERE seven_d_utilization IS NOT NULL AND ts >= ? ORDER BY ts",
        (since,)).fetchall()
    found, examined = [], 0
    for prev, row in zip(rows, rows[1:]):
        if row["r"] != prev["r"] or not prev["u"] or not row["u"]:
            continue  # the window rolled over; utilisation legitimately drops
        examined += 1
        spent = _credits_between(conn, prev["ts"], row["ts"])
        # 0.01 of quantisation on each reading, plus whatever traffic explains.
        explained = spent / VISIBLE_SHARE / cap + 0.02
        if abs(row["u"] - prev["u"]) > explained:
            found.append((prev["ts"], row["ts"], prev["u"], row["u"], spent))
    return found, examined


def _ratio_bounds(pairs):
    """Tightest constant ratio consistent with every (7d, 7d_oi) pair.

    Both meters round to two decimals, so each pair only pins the ratio to an
    interval. A non-empty intersection means one constant explains them all --
    that is, they share a numerator and differ only in denominator.
    """
    low, high = 0.0, math.inf
    for base, oi in pairs:
        low = max(low, (oi - 0.005) / (base + 0.005))
        high = min(high, (oi + 0.005) / (base - 0.005))
    return low, high


def calibrate(args):
    """Estimate the weekly denominator and flag it changing under us."""
    if not args.db.exists():
        print("no database yet", file=sys.stderr)
        return 1
    with connect(args.db, readonly=True) as conn:
        latest = conn.execute(
            "SELECT * FROM ratelimit WHERE seven_d_utilization IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1").fetchone()
        if latest is None:
            print("no 7d signal recorded yet", file=sys.stderr)
            return 1
        reset = latest["seven_d_reset"] or int(time.time())
        start = reset - 7 * 86400
        spent = _credits_between(conn, start, latest["ts"])
        first = conn.execute(
            "SELECT MIN(ts) t FROM requests WHERE model IS NOT NULL "
            "AND model != '' AND ts >= ?", (start,)).fetchone()["t"]
        pairs = sorted({(r["seven_d_utilization"], _oi(r)) for r in conn.execute(
            "SELECT seven_d_utilization, extra FROM ratelimit "
            "WHERE seven_d_utilization IS NOT NULL")
            if r["seven_d_utilization"] and _oi(r)})
        shifts, examined = _shifts(conn, args.cap)

    util = latest["seven_d_utilization"]
    print(f"7d window : {_local(start)} -> {_local(reset)}")
    print(f"reported  : {util * 100:.0f}% at {_local(latest['ts'])}"
          f"   (claim: {latest['representative_claim'] or '-'})")
    print(f"recorded  : {spent:,} credits, this machine only")

    # recorded/reported only bounds the denominator if the proxy was running
    # for the whole window; otherwise it is missing days, not just machines.
    partial = first is None or first > start + 3600
    if partial:
        missed = (first - start) / 86400 if first else 7.0
        print(f"\ndenominator : no bound -- the proxy missed the first "
              f"{missed:.1f} days of\n  this window, so recorded credits are "
              f"not comparable to the reported figure.")
    else:
        floor = spent / util if util else 0
        print(f"\ndenominator >= {floor:,.0f} credits (recorded / reported; a "
              f"floor, since\n  the proxy never sees the whole account)")
        for mult in (1.0, 1.25, 1.5):
            cap = NOMINAL_WEEKLY * mult
            note = "  <- ruled out by the floor" if cap < floor else ""
            print(f"    {mult:>4}x nominal = {cap:>13,.0f}{note}")

    if pairs:
        low, high = _ratio_bounds(pairs)
        print(f"\n7d vs 7d_oi, {len(pairs)} distinct paired readings:")
        print("    " + "  ".join(f"{b:.2f}/{o:.2f}" for b, o in pairs))
        if low <= high:
            print(f"  one constant ratio fits every pair: [{low:.3f}, {high:.3f}]")
            print(f"  -> same numerator, two denominators; at 1.2 that is "
                  f"{NOMINAL_WEEKLY * 1.5:,.0f} and {NOMINAL_WEEKLY * 1.25:,.0f}")
        else:
            print("  no single ratio fits: the meters differ in more than "
                  "their denominator")

    if shifts:
        print("\nDENOMINATOR SHIFT")
        for t0, t1, u0, u1, between in shifts:
            gap = (t1 - t0) / 3600
            print(f"  {_local(t0)} -> {_local(t1)} ({gap:.1f}h apart)")
            print(f"    7d {u0 * 100:.0f}% -> {u1 * 100:.0f}% on {between:,} "
                  f"credits of recorded traffic")
            print(f"    denominator moved x{u0 / u1:.3f}  "
                  f"({args.cap:,.0f} -> {args.cap * u0 / u1:,.0f} if it started there)")
    else:
        print(f"\nno unexplained jump across {examined} sample gaps: the "
              f"denominator has held steady\n  throughout everything recorded.")
    return 0


# --------------------------------------------------------------------------- #

COMMANDS = ("serve", "install", "uninstall", "status", "history", "usage",
            "calibrate", "cert", "trust")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help")):
        argv.insert(0, "serve")  # bare `proxy.py` runs the proxy

    # --db / --port belong to every subcommand, so they may follow it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=DB_PATH)
    common.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="plain-HTTP fallback listener")
    common.add_argument("--tls-port", type=int, default=DEFAULT_TLS_PORT,
                        help="TLS listener the hosts entry points at")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name, help_text, **kwargs):
        return sub.add_parser(name, help=help_text, parents=[common], **kwargs)

    run = add("serve", "run the proxy in the foreground")
    run.add_argument("--timeout", type=int, default=900)
    run.add_argument("--verbose", action="store_true")
    run.set_defaults(func=serve)

    add("install", "install and start the launchd agent").set_defaults(func=install)
    add("uninstall", "stop and remove the launchd agent").set_defaults(func=uninstall)
    add("status", "agent state, health and latest signal").set_defaults(func=status)
    add("trust", "print the sudo steps for the CA and /etc/hosts").set_defaults(func=trust)

    certs = add("cert", "create or renew the CA and leaf certificate")
    certs.add_argument("--force", action="store_true",
                       help="regenerate the CA too; it must then be re-trusted")
    certs.set_defaults(func=cert)

    hist = add("history", "utilisation samples over time")
    hist.add_argument("--days", type=float, default=7)
    hist.set_defaults(func=history)

    use = add("usage", "tokens and credits by model")
    use.add_argument("--days", type=float, default=7)
    use.set_defaults(func=usage)

    cal = add("calibrate", "infer the weekly denominator, flag it changing")
    cal.add_argument("--cap", type=float, default=PROMO_WEEKLY,
                     help="denominator believed in force, for the jump test")
    cal.set_defaults(func=calibrate)

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
