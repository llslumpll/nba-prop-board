"""Short, readable error text for pages and logs."""

import re


def short(e: Exception) -> str:
    """'ConnectionError: could not reach api.example.com' instead of the
    full urllib3 retry chain. Never includes query strings (keys live there)."""
    msg = str(e)
    host = re.search(r"host='([^']+)'", msg)
    name = type(e).__name__
    if name in ("ProxyError", "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout", "SSLError"):
        return f"{name}: could not reach {host.group(1) if host else 'the server'}"
    status = re.search(r"(\d{3}) (?:Client|Server) Error", msg)
    if status:
        return f"HTTP {status.group(1)} from {host.group(1) if host else 'the server'}"
    msg = re.sub(r"\?[^\s'\"]*", "", msg)  # drop query strings
    return f"{name}: {msg[:140]}"
