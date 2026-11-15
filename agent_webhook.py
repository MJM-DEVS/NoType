"""
NoType – Agent Hotkey
Sends a finished dictation to a URL of the user's choice (HTTP POST, JSON)
instead of typing it, for example to an n8n webhook or a self-hosted
assistant, and turns the answer into one short line for the overlay.

Stdlib only. backend.py calls post() on a worker thread, never on the stdin
loop or the audio thread. post() never raises.

What is logged: the host, the status code, the timing and the size of the
answer. Never the dictated text, the token, the answer or the full URL
(webhook URLs often carry a secret in their path or query).
"""

import html
import ipaddress
import json
import logging
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import loopback

logger = logging.getLogger("NoType.Agent")

TIMEOUT_S = 15                  # per connect/read, like urllib's own timeout
MAX_ANSWER_BYTES = 64 * 1024    # read cap for the answer
MAX_SHOWN_CHARS = 200           # the overlay shows at most this much of it
ANSWER_FIELDS = ("message", "reply", "text")   # JSON fields, first non-empty wins

_SPACE = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]*>")


def local_timestamp() -> str:
    """Now as ISO 8601 with the local UTC offset (2026-10-12T14:03:22+02:00)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def build_payload(text: str, language: str = "", context: dict | None = None) -> dict:
    """The JSON body for a dictation. `context` only when the user allows it."""
    payload = {
        "text": text,
        "source": "notype",
        "language": language or "",
        "timestamp": local_timestamp(),
    }
    if context is not None:
        payload["context"] = context
    return payload


def build_test_payload() -> dict:
    """The JSON body of the "Send test" button in the settings."""
    return {"text": "NoType test", "source": "notype", "test": True,
            "timestamp": local_timestamp()}


def _shorten(text: str) -> str:
    """One line (the overlay has no line breaks), at most MAX_SHOWN_CHARS."""
    text = _SPACE.sub(" ", text).strip()
    if len(text) > MAX_SHOWN_CHARS:
        text = text[:MAX_SHOWN_CHARS - 1].rstrip() + "…"
    return text


def extract_message(body: bytes, content_type: str = "", charset: str | None = None) -> str:
    """The line to show for a 2xx answer, '' when there is nothing to show
    (the overlay then just says "Sent").

    A JSON object: its `message`, else `reply`, else `text` (strings only).
    A JSON string: that string. Plain text: its start. HTML: its text."""
    try:
        raw = body.decode(charset or "utf-8", "replace").strip()
    except LookupError:  # unknown charset name in the header
        raw = body.decode("utf-8", "replace").strip()
    if not raw:
        return ""
    ctype = (content_type or "").lower()
    try:
        data, is_json = json.loads(raw), True
    except ValueError:
        data, is_json = None, False
    if isinstance(data, dict):
        for key in ANSWER_FIELDS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return _shorten(value)
        return ""
    if is_json and "json" in ctype:
        return _shorten(data) if isinstance(data, str) else ""
    if "html" in ctype:
        raw = html.unescape(_TAG.sub(" ", raw))
    return _shorten(raw)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are not followed: urllib would hand the Authorization header
    to whatever host the redirect points at. A 3xx counts as an error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)
# A webhook on this machine (n8n on localhost, …) is reached directly, never
# through a system proxy.
_local_opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def _host(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


def _result(ok: bool, status: int | None = None, message: str = "", error: str = "") -> dict:
    return {"ok": ok, "status": status, "message": message, "error": error}


def _is_timeout(err) -> bool:
    return isinstance(err, (TimeoutError, socket.timeout))


def _is_private_host(host: str | None) -> bool:
    """True for this machine and the local network: loopback, private and
    link-local addresses, *.local and single-label names (NAS, WSL host)."""
    host = (host or "").strip("[]").rstrip(".").lower()
    if loopback.is_loopback_host(host):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(".local") or (bool(host) and "." not in host)
    return ip.is_private or ip.is_link_local or ip.is_loopback


def post(url: str, payload: dict, token: str = "", version: str = "",
         timeout: float = TIMEOUT_S) -> dict:
    """POST `payload` as JSON to `url`. Never raises.

    Returns {"ok", "status", "message", "error"}:
      ok       True for a 2xx answer
      status   HTTP status code, None when there was no answer
      message  the line to show (see extract_message), '' for none
      error    '' | no_url | bad_url | insecure_url | http | timeout | tls | unreachable

    A token only goes out over https, or over http to this machine or the
    local network (see _is_private_host). A plain http URL to a public host
    with a token set counts as insecure_url.
    """
    url = (url or "").strip()
    if not url:
        return _result(False, error="no_url")
    try:
        parts = urllib.parse.urlsplit(url)
        valid = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        valid = False
    if not valid:
        return _result(False, error="bad_url")
    local = loopback.is_loopback_host(parts.hostname)
    token = (token or "").strip()
    if token and parts.scheme == "http" and not _is_private_host(parts.hostname):
        logger.warning(f"Agent: not sending the token over plain http to {parts.hostname}, "
                       "use https (or a local network address)")
        return _result(False, error="insecure_url")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"NoType/{version}" if version else "NoType",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")

    host = _host(url)
    started = time.perf_counter()
    ms = lambda: int((time.perf_counter() - started) * 1000)  # noqa: E731
    try:
        with (_local_opener if local else _opener).open(request, timeout=timeout) as resp:
            status = resp.status
            answer = resp.read(MAX_ANSWER_BYTES)
            ctype = resp.headers.get("Content-Type", "")
            charset = resp.headers.get_content_charset()
    except urllib.error.HTTPError as e:
        logger.warning(f"Agent: {host} answered HTTP {e.code} ({ms()} ms)")
        try:
            e.close()
        except Exception:
            pass
        return _result(False, status=e.code, error="http")
    except urllib.error.URLError as e:
        reason = e.reason
        if _is_timeout(reason):
            logger.warning(f"Agent: {host} timed out ({ms()} ms)")
            return _result(False, error="timeout")
        if isinstance(reason, ssl.SSLError):
            logger.warning(f"Agent: TLS error with {host}: {type(reason).__name__} ({ms()} ms)")
            return _result(False, error="tls")
        logger.warning(f"Agent: {host} unreachable: {type(reason).__name__} ({ms()} ms)")
        return _result(False, error="unreachable")
    except Exception as e:  # timeouts while reading, dropped connections, bad answers
        if _is_timeout(e):
            logger.warning(f"Agent: {host} timed out ({ms()} ms)")
            return _result(False, error="timeout")
        logger.warning(f"Agent: {host} failed: {type(e).__name__} ({ms()} ms)")
        return _result(False, error="unreachable")

    if not 200 <= status < 300:  # urllib raises for these; just in case
        logger.warning(f"Agent: {host} answered HTTP {status} ({ms()} ms)")
        return _result(False, status=status, error="http")
    try:
        message = extract_message(answer, ctype, charset)
    except Exception as e:
        logger.warning(f"Agent: reading the answer failed: {type(e).__name__}")
        message = ""
    logger.info(f"Agent: {host} answered HTTP {status} ({ms()} ms, {len(answer)} bytes)")
    return _result(True, status=status, message=message)
