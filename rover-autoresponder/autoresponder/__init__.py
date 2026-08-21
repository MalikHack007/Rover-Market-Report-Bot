"""Rover auto-responder."""

# Prefer IPv4 for outbound HTTP before any client library is constructed. On a host with
# a broken IPv6 route this is the difference between ~16s and ~0.5s per request.
try:  # pragma: no cover - environment dependent
    from . import netprefs
    netprefs.apply()
except Exception:  # never let a network tweak break startup
    pass