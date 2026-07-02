"""Shared HTTP session factory: keep-alive pooling + polite retries.

A retrying adapter backs off on 429/5xx and honours ``Retry-After``, so a
momentary upstream hiccup no longer silently drops a page of promos (the old
behaviour was "log the error and lose the page"). Reused TCP/TLS connections are
also lighter on the upstream than reconnecting per request.

Scrapers keep their per-thread session pattern; they just build the session
through here instead of bare ``requests.Session()``.
"""
from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Global ceiling on simultaneous outbound HTTP requests across ALL chains.
#
# Running every chain at once otherwise peaks at ~150-200 concurrent sockets from
# a single IP. A test run showed fora and varus then start dropping connections
# (BrokenPipe / ConnectionReset / SSL-EOF) and lose whole stores, while silpo
# (plain GET) stayed fine. The old sequential code only ever had ~one chain's
# worth of sockets in flight (~50) and was stable, so we cap the AGGREGATE near
# that level. Every scraper's lowest-level request must acquire this gate, or the
# cap leaks (e.g. the national-catalog scrapers' internal page pools).
#
# Tunable without code edits via HTTP_MAX_CONCURRENCY so we can find the safe
# ceiling empirically on re-test.
MAX_CONCURRENCY = int(os.getenv("HTTP_MAX_CONCURRENCY", "48"))
REQUEST_GATE = threading.BoundedSemaphore(MAX_CONCURRENCY)

# Per-host circuit breaker. On a "bad host day" (an API dropping every connection
# — e.g. fora returning BrokenPipe/timeout en masse) the retry layers otherwise
# turn each store into minutes of futile re-tries, dragging a ~30-min run into
# hours. After CB_CONSEC consecutive CONNECTION failures to a host the breaker
# "opens": further requests to it fail INSTANTLY (no wait, no retry) for
# CB_COOLDOWN seconds, so the chain finishes fast with partial data instead of
# grinding. A single success closes it (self-healing). 5xx does NOT count — the
# connection worked; only real connection errors (raised) do. Tunable via env.
CB_CONSEC = int(os.getenv("CIRCUIT_FAIL_LIMIT", "15"))
CB_COOLDOWN = int(os.getenv("CIRCUIT_COOLDOWN", "90"))
_CB_LOCK = threading.Lock()
_cb: dict = {}  # host -> [consecutive_conn_failures, open_until_monotonic]


def circuit_open(host: str) -> bool:
    st = _cb.get(host)
    return bool(st and time.monotonic() < st[1])


def note_result(host: str, ok: bool) -> None:
    with _CB_LOCK:
        st = _cb.setdefault(host, [0, 0.0])
        if ok:
            st[0] = 0
            st[1] = 0.0  # a success closes the circuit immediately
        else:
            st[0] += 1
            if st[0] >= CB_CONSEC:
                st[1] = time.monotonic() + CB_COOLDOWN


def _host(url: str) -> str:
    return urlparse(url).hostname or url


class _GatedHTTPAdapter(HTTPAdapter):
    """Gates every request on the global REQUEST_GATE (cap on concurrent sockets)
    and on the per-host circuit breaker (fail fast when a host is down). Mounted
    by make_session(), so all requests-based scrapers get both transparently;
    scrapers with their own client wire circuit_open/note_result manually."""

    def send(self, request, *args, **kwargs):
        host = _host(request.url)
        if circuit_open(host):
            raise requests.exceptions.ConnectionError(f"circuit breaker open for {host}")
        with REQUEST_GATE:
            try:
                resp = super().send(request, *args, **kwargs)
            except Exception:
                note_result(host, False)  # connection-level failure
                raise
        note_result(host, True)  # got a response (even 5xx) → connection healthy
        return resp


def make_session(headers: dict | None = None, pool_maxsize: int = 10) -> requests.Session:
    """A requests.Session with connection pooling and backoff retries.

    POST is in ``allowed_methods`` because every POST we make is a read-only
    catalog query (GraphQL / exec), which is safe to retry. ``raise_on_status``
    is False so the caller's own ``raise_for_status()`` still decides what to do
    after retries are exhausted — preserving existing error handling.
    """
    s = requests.Session()
    if headers:
        s.headers.update(headers)
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.6,  # waits ~0.6s, 1.2s, 2.4s between attempts
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = _GatedHTTPAdapter(
        max_retries=retry,
        pool_connections=pool_maxsize,
        pool_maxsize=pool_maxsize,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s
