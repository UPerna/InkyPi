"""
HTTP Client with Connection Pooling for InkyPi

Provides a shared requests.Session() instance for all plugins to use.
Benefits:
- Connection reuse (20-30% faster requests)
- Reduced TCP handshake overhead
- Automatic keep-alive handling
- Consistent headers across all requests

Usage:
    from utils.http_client import get_http_session

    session = get_http_session()
    response = session.get(url)
"""

import requests
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Global session instance (singleton)
_HTTP_SESSION: Optional[requests.Session] = None


def get_http_session() -> requests.Session:
    """
    Get the shared HTTP session instance.
    Creates it on first call (lazy initialization).

    Returns:
        requests.Session: Shared session with connection pooling
    """
    global _HTTP_SESSION

    if _HTTP_SESSION is None:
        logger.debug("Initializing shared HTTP session with connection pooling")
        _HTTP_SESSION = requests.Session()

        # Set common headers for all InkyPi requests
        _HTTP_SESSION.headers.update({
            'User-Agent': 'InkyPi/1.0 (https://github.com/fatihak/InkyPi/)'
        })

        # Configure connection pool
        # Max 10 connections per host (reasonable for e-ink device)
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
            max_retries=3,
            pool_block=False
        )
        _HTTP_SESSION.mount('http://', adapter)
        _HTTP_SESSION.mount('https://', adapter)

        logger.debug("HTTP session initialized successfully")

    return _HTTP_SESSION


def close_http_session():
    """
    Close the shared HTTP session.
    Should be called on application shutdown.
    """
    global _HTTP_SESSION

    if _HTTP_SESSION is not None:
        logger.debug("Closing shared HTTP session")
        _HTTP_SESSION.close()
        _HTTP_SESSION = None



def bounded_chunks(response, limit, deadline):
    """Yield decoded chunks within the byte and transfer-time budgets."""
    headers = getattr(response, "headers", {})
    length = headers.get("Content-Length")
    length = int(length) if length is not None else None
    if length is not None and not 0 <= length <= limit:
        raise ValueError("Response exceeds the byte limit")

    raw = response.raw
    if hasattr(raw, "read1") and hasattr(raw, "decode_content"):
        # Return available data so a slow sender cannot defer every deadline check
        # until an entire 64 KiB chunk has arrived.
        chunks = iter(lambda: raw.read1(65536, decode_content=True), b"")
    else:
        chunks = response.iter_content(chunk_size=65536)
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > limit:
            raise ValueError("Response exceeds the byte limit")
        if time.monotonic() >= deadline:
            raise TimeoutError("Response deadline reached")
        yield chunk
    if time.monotonic() >= deadline:
        raise TimeoutError("Response deadline reached")
    if length is not None and headers.get("Content-Encoding", "identity").lower() == "identity" and total != length:
        raise ValueError("Incomplete response")

