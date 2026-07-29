"""Tests for the Phase 3 fix: get_message 404 handling + history INBOX filter."""
import httplib2
import pytest

from googleapiclient.errors import HttpError

from autoresponder import gmail_client


# --- fake Gmail service chain: service.users().messages().get(...).execute() ---
class _Exec:
    def __init__(self, result=None, exc=None): self._result = result; self._exc = exc
    def execute(self):
        if self._exc:
            raise self._exc
        return self._result

class _Messages:
    def __init__(self, result=None, exc=None): self._e = _Exec(result, exc)
    def get(self, **kwargs): return self._e

class _History:
    def __init__(self, captured): self._captured = captured
    def list(self, **kwargs):
        self._captured.update(kwargs)
        return _Exec(result={"history": []})

class _Users:
    def __init__(self, msgs=None, hist=None): self._msgs = msgs; self._hist = hist
    def messages(self): return self._msgs
    def history(self): return self._hist

class _Service:
    def __init__(self, msgs=None, hist=None): self._u = _Users(msgs, hist)
    def users(self): return self._u


def _http_error(status):
    resp = httplib2.Response({"status": status})
    resp.reason = "Not Found" if status == 404 else "Error"
    return HttpError(resp, b'{"error": {"message": "x"}}')


def test_get_message_returns_dict_on_success():
    svc = _Service(msgs=_Messages(result={"id": "m1", "payload": {}}))
    assert gmail_client.get_message(svc, "m1") == {"id": "m1", "payload": {}}


def test_get_message_returns_none_on_404():
    svc = _Service(msgs=_Messages(exc=_http_error(404)))
    assert gmail_client.get_message(svc, "gone") is None


def test_get_message_reraises_non_404():
    svc = _Service(msgs=_Messages(exc=_http_error(500)))
    with pytest.raises(HttpError):
        gmail_client.get_message(svc, "m1")


def test_list_history_filters_to_inbox():
    captured = {}
    svc = _Service(hist=_History(captured))
    gmail_client.list_history(svc, "123")
    assert captured.get("labelId") == "INBOX"
    assert captured.get("historyTypes") == ["messageAdded"]
