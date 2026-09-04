"""Prefer IPv4 for outbound HTTP (subsystem 1 copy).

Same fix as ``rover-autoresponder/autoresponder/netprefs.py``, kept as a small
self-contained copy here so the rank bot has no dependency on the auto-responder
package tree (the two subsystems are independent processes). If you change the
resolution policy, change it in both.

Why: hosts we talk to (googleapis.com for the Gmail send) publish both A and AAAA
records. On this bridged VirtualBox guest there's an IPv6 address but no working IPv6
route, so Python tries IPv6 FIRST and sequentially, stalling through the TCP SYN retry
ladder (1+2+4+8s ~= 15-16s) before falling back to IPv4. That intermittently blew past
the Gmail send's socket timeout -> "email failed: timed out". curl was unaffected
because it does Happy Eyeballs (races both families); Python's socket layer does not.

Importing this module and calling apply() makes name resolution return A records only,
for EVERY library in the process -- the Google API client uses httplib2, which resolves
addresses itself, so a urllib3-only patch isn't enough.

Set PREFER_IPV4=0 to disable, e.g. if the host ever becomes genuinely IPv6-only.
"""
import os
import socket

_applied = False
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Resolve unspecified-family lookups as IPv4 only.

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
        pass  # urllib3 not present; socket-level patch still applies

    _applied = True
    return True


def restore():
    """Undo the patch (tests)."""
    global _applied
    socket.getaddrinfo = _orig_getaddrinfo
    _applied = False
