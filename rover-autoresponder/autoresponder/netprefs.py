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

Importing this module makes urllib3 (and therefore requests) resolve A records only.
Set PREFER_IPV4=0 to disable, e.g. if the host ever becomes genuinely IPv6-only.
"""
import logging
import os
import socket

log = logging.getLogger(__name__)

_applied = False


def apply(force=None):
    """Restrict urllib3's address resolution to IPv4. Idempotent."""
    global _applied
    if _applied:
        return True
    enabled = force if force is not None else (
        os.environ.get("PREFER_IPV4", "1").strip().lower() not in ("0", "false", "no"))
    if not enabled:
        return False
    try:
        import urllib3.util.connection as urllib3_connection
        urllib3_connection.allowed_gai_family = lambda: socket.AF_INET
    except Exception:
        log.exception("could not force IPv4 for urllib3")
        return False
    _applied = True
    log.info("outbound HTTP restricted to IPv4 (avoids broken-IPv6 connect stalls)")
    return True
