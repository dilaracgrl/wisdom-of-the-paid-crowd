"""Shared HTTP session: retries, backoff, and polite rate limiting.

Neither Polymarket nor Kalshi publishes hard limits for the public read
endpoints we use, so we self-throttle to a modest request rate and back off
on 429/5xx rather than risk getting blocked mid-pull.
"""
from __future__ import annotations

import time
from threading import Lock

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class RateLimitedSession:
    """A requests.Session wrapper enforcing a minimum interval between calls."""

    def __init__(self, min_interval: float = 0.2, timeout: float = 30.0):
        self.min_interval = min_interval
        self.timeout = timeout
        self._last = 0.0
        self._lock = Lock()

        self.session = requests.Session()
        retry = Retry(
            total=5,
            backoff_factor=1.0,  # 1s, 2s, 4s, 8s, 16s
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": "wisdom-of-the-paid-crowd/0.1 (research)"})

    def get_json(self, url: str, params: dict | None = None):
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()
