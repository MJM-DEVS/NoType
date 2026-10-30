"""
NoType – Loopback HTTP
Requests to services on this machine (Ollama on 127.0.0.1:11434, a local
agent webhook) must never leave it. urllib's default opener honours the
system proxy (HTTP(S)_PROXY, on Windows also the Internet Options in the
registry) and the registry bypass list usually doesn't cover 127.0.0.1, so
a dictation sent to the local LLM could end up at a corporate proxy.

Downloads from the internet (GitHub, models) keep the normal proxy setup.
"""

import ipaddress
import urllib.parse
import urllib.request

# ProxyHandler({}): no proxies at all, whatever the environment or registry says.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_loopback_host(host: str | None) -> bool:
    """True for localhost, 127.0.0.0/8 and ::1."""
    host = (host or "").strip("[]").rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_loopback_url(url: str) -> bool:
    try:
        return is_loopback_host(urllib.parse.urlsplit(url).hostname)
    except ValueError:
        return False


def urlopen(url, timeout: float, data=None):
    """urllib.request.urlopen for loopback URLs only, never via a proxy.
    `url` is a string or a Request. Anything else is refused."""
    full = url.full_url if isinstance(url, urllib.request.Request) else url
    if not is_loopback_url(full):
        raise ValueError("loopback.urlopen is for local addresses only")
    return _opener.open(url, data=data, timeout=timeout)
