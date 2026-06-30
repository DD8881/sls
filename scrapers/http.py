"""Shared HTTP session factory: keep-alive pooling + polite retries.

A retrying adapter backs off on 429/5xx and honours ``Retry-After``, so a
momentary upstream hiccup no longer silently drops a page of promos (the old
behaviour was "log the error and lose the page"). Reused TCP/TLS connections are
also lighter on the upstream than reconnecting per request.

Scrapers keep their per-thread session pattern; they just build the session
through here instead of bare ``requests.Session()``.
"""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


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
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=pool_maxsize,
        pool_maxsize=pool_maxsize,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s
