"""One TLS context for every HTTP client the program makes. httpx builds a new one for each client otherwise, loading certifi's
certificates each time (about 35 ms apiece, on the event loop, up to six times before the service listens)."""

import ssl
from functools import cache

import httpx


@cache
def ssl_context() -> ssl.SSLContext:
    """The context httpx would make for a client of its own (certifi's certificates, or SSL_CERT_FILE / SSL_CERT_DIR when set), made
    once, on first use, and shared: pass it as `verify=`."""
    return httpx.create_ssl_context()
