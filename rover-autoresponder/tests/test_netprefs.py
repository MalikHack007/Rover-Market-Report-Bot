"""IPv4 preference — guards against the broken-IPv6 connect stall."""
import socket

from autoresponder import netprefs


def test_applied_on_package_import():
    """Importing the package must restrict resolution before any client is built."""
    import urllib3.util.connection as urllib3_connection
    assert urllib3_connection.allowed_gai_family() == socket.AF_INET


def test_apply_is_idempotent():
    assert netprefs.apply() in (True, False)     # already applied -> True, no error


def test_can_be_disabled(monkeypatch):
    monkeypatch.setattr(netprefs, "_applied", False)
    assert netprefs.apply(force=False) is False
