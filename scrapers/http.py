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


class _GatedHTTPAdapter(HTTPAdapter):
    """Holds a global permit for the duration of each request, so the total
    in-flight requests across every session and thread never exceeds
    MAX_CONCURRENCY. make_session() mounts this, so all requests-based scrapers
    are gated transparently; scrapers with their own client wrap REQUEST_GATE
    around their request call directly."""

    def send(self, *args, **kwargs):
        with REQUEST_GATE:
            return super().send(*args, **kwargs)


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
