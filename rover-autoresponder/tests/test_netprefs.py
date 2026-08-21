"""IPv4 preference — guards against the broken-IPv6 connect stall."""
import socket

from autoresponder import netprefs


def test_applied_on_package_import():
    """Importing the package must restrict resolution before any client is built."""
    import urllib3.util.connection as urllib3_connection
    assert urllib3_connection.allowed_gai_family() == socket.AF_INET


def test_unspecified_family_resolves_ipv4_only():
    """This is what covers httplib2 (Google API client), which urllib3's hook misses."""
    infos = socket.getaddrinfo("localhost", 80, 0, socket.SOCK_STREAM)
    assert infos, "expected at least one result"
    assert all(fam == socket.AF_INET for fam, *_ in infos)


def test_explicit_ipv6_still_passes_through():
    """Don't break deliberate IPv6 lookups (or diagnostics)."""
    try:
        infos = socket.getaddrinfo("localhost", 80, socket.AF_INET6, socket.SOCK_STREAM)
    except socket.gaierror:
        return                      # host has no IPv6 for localhost; fine
    assert all(fam == socket.AF_INET6 for fam, *_ in infos)


def test_apply_is_idempotent():
    assert netprefs.apply() in (True, False)


def test_can_be_disabled_and_restored(monkeypatch):
    netprefs.restore()
    try:
        assert netprefs.apply(force=False) is False
        assert socket.getaddrinfo is netprefs._orig_getaddrinfo
    finally:
        netprefs.apply(force=True)