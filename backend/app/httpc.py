"""Rate-limited, budget-aware HTTP client.

Every source gets a token-bucket rate limiter; downloads respect the per-
investigation byte budget. Retries are bounded with exponential backoff.
This is the single chokepoint for all outbound network traffic.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

from .config import settings
from . import db


class TokenBucket:
    def __init__(self, rps: float):
        self.capacity = max(1.0, rps * 2)
        self.tokens = self.capacity
        self.rate = rps
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(min(wait, 1.0))


def _bucket_for(url: str) -> float:
    host = urlparse(url).netloc.lower()
    if "openalex" in host:
        return settings.rps_openalex
    if "crossref" in host:
        return settings.rps_crossref
    if "europepmc" in host or "ebi.ac.uk" in host:
        return settings.rps_europepmc
    return settings.rps_pdf


_buckets: dict[str, TokenBucket] = {}
_buckets_lock = threading.Lock()


def _bucket(url: str) -> TokenBucket:
    key = urlparse(url).netloc.lower()
    # collapse to source family so limits are per-source, not per-mirror
    fam = ("openalex" if "openalex" in key else
           "crossref" if "crossref" in key else
           "europepmc" if ("europepmc" in key or "ebi.ac.uk" in key) else
           "pdf")
    with _buckets_lock:
        if fam not in _buckets:
            _buckets[fam] = TokenBucket(_bucket_for(url))
        return _buckets[fam]


class FetchError(Exception):
    pass


def _client() -> httpx.Client:
    headers = {"User-Agent": settings.user_agent}
    if settings.contact_email:
        headers["User-Agent"] = f"{settings.user_agent}; mailto:{settings.contact_email}"
    return httpx.Client(headers=headers, timeout=settings.http_timeout, follow_redirects=True)


def get_json(url: str, params: Optional[dict] = None) -> dict:
    return _get(url, params, accept="application/json").json()


def get_text(url: str, params: Optional[dict] = None) -> str:
    return _get(url, params, accept="application/xml,text/xml,*/*").text


def _get(url: str, params: Optional[dict], accept: str) -> httpx.Response:
    if params and "mailto" not in params and settings.contact_email and (
            "openalex" in url or "crossref" in url):
        params = {**params, "mailto": settings.contact_email}
    last_exc: Optional[Exception] = None
    for attempt in range(1, settings.http_retries + 1):
        _bucket(url).wait()
        try:
            with _client() as client:
                resp = client.get(url, params=params, headers={"Accept": accept})
                if resp.status_code == 200:
                    return resp
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_exc = FetchError(f"HTTP {resp.status_code} for {url}")
                    time.sleep(min(2 ** attempt + random.random(), 30))
                    continue
                raise FetchError(f"HTTP {resp.status_code} for {url}")
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            last_exc = e
            time.sleep(min(2 ** attempt + random.random(), 30))
    raise FetchError(f"exhausted retries for {url}: {last_exc}")


def download(url: str, dest: "Path", inv_id: Optional[int] = None, max_mb: Optional[int] = None) -> int:
    """Download a file, respecting the investigation byte budget. Returns bytes written."""
    cap = (max_mb if max_mb is not None else settings.max_download_mb) * 1024 * 1024
    last_exc: Optional[Exception] = None
    for attempt in range(1, settings.http_retries + 1):
        _bucket(url).wait()
        try:
            with _client() as client:
                with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        if resp.status_code in (429, 500, 502, 503, 504):
                            last_exc = FetchError(f"HTTP {resp.status_code}")
                            time.sleep(min(2 ** attempt + random.random(), 30))
                            continue
                        raise FetchError(f"HTTP {resp.status_code} for {url}")
                    total = 0
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with open(dest, "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            total += len(chunk)
                            if total > cap:
                                f.close()
                                dest.unlink(missing_ok=True)
                                raise FetchError(f"download exceeds budget ({cap // 1048576} MB): {url}")
                            f.write(chunk)
                    if inv_id is not None:
                        db.bump_counter(inv_id, "bytes_downloaded", total)
                    return total
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            last_exc = e
            time.sleep(min(2 ** attempt + random.random(), 30))
    raise FetchError(f"download failed after retries for {url}: {last_exc}")
