"""Prefer IPv4 for outbound HTTP.

Hosts we talk to (api.telegram.org, api.cal.com, googleapis.com) publish both A and AAAA
records. If the machine has an IPv6 address but no working IPv6 route — the usual case
for a bridged VirtualBox guest — Python tries IPv6 FIRST and sequentially, so every
request stalls through the TCP SYN retry ladder (1+2+4+8s ≈ 15-16s) before falling back
to IPv4.

That was the cause of: Telegram button taps hanging ~16s (past Telegram's ~15s callback
expiry, so the spinner never resolved), cal.com read timeouts, and getUpdates handshake
timeouts. curl looked fine throughout because it implements Happy Eyeballs — racing both
families — which Python's socket layer does not.

Importing this module makes name resolution return A records only, for EVERY library in
the process — requests/urllib3 and httplib2 (which the Google API client uses) resolve
separately, so patching one isn't enough.

Set PREFER_IPV4=0 to disable, e.g. if the host ever becomes genuinely IPv6-only.
"""
import logging
import os
import socket

log = logging.getLogger(__name__)

_applied = False
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Resolve unspecified-family lookups as IPv4 only.

    Patching urllib3 alone isn't enough: the Google API client uses **httplib2**, which
    resolves addresses itself, so calendar writes still stalled on IPv6. Doing it here
    covers every library in the process.

    An explicit AF_INET6 request is passed through untouched, so genuine IPv6 lookups
    (and diagnostics) still work.
    """
    if family == 0:                       # AF_UNSPEC -> caller has no preference
        family = socket.AF_INET
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


def apply(force=None):
    """Restrict outbound name resolution to IPv4. Idempotent."""
    global _applied
    if _applied:
        return True
    enabled = force if force is not None else (
        os.environ.get("PREFER_IPV4", "1").strip().lower() not in ("0", "false", "no"))
    if not enabled:
        return False

    # 1. Process-wide: covers httplib2 (Google API client), and anything else.
    socket.getaddrinfo = _ipv4_only_getaddrinfo
    # 2. urllib3/requests also consults this hook directly.
    try:
        import urllib3.util.connection as urllib3_connection
        urllib3_connection.allowed_gai_family = lambda: socket.AF_INET
    except Exception:
        log.debug("urllib3 not present; socket-level patch still applies")

    _applied = True
    log.info("outbound name resolution restricted to IPv4 "
             "(avoids broken-IPv6 connect stalls)")
    return True


def restore():
    """Undo the patch (tests)."""
    global _applied
    socket.getaddrinfo = _orig_getaddrinfo
    _applied = False