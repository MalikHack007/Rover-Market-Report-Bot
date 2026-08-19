"""S1: SMS send adapter, HMAC verification, dedup, and end-to-end receiver."""
import hashlib
import hmac
import json
import threading
import time

import requests

from autoresponder import sms_gateway, sms_receiver, config


# --- outbound send adapter ---
class _Resp:
    def __init__(self, code, payload=None): self.status_code = code; self._p = payload or {}; self.text = ""
    def json(self): return self._p

def test_send_builds_correct_request(monkeypatch):
    captured = {}
    def fake_post(url, params=None, json=None, auth=None, timeout=None):
        captured.update(url=url, params=params, json=json)
        return _Resp(202, {"id": "gw-123", "state": "Pending"})
    monkeypatch.setattr(sms_gateway.requests, "post", fake_post)
    gw = sms_gateway.SmsGateForAndroid(base_url="http://phone:8080", username="u", password="p")
    mid = gw.send("+15551234567", "hello there", message_id="idem-1")
    assert mid == "gw-123"
    assert captured["url"] == "http://phone:8080/message"   # LOCAL mode: bare path
    assert captured["params"]["skipPhoneValidation"] == "true"
    assert captured["json"]["textMessage"]["text"] == "hello there"
    assert captured["json"]["phoneNumbers"] == ["+15551234567"]
    assert captured["json"]["id"] == "idem-1"

def test_send_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(sms_gateway.requests, "post",
                        lambda *a, **k: _Resp(500))
    gw = sms_gateway.SmsGateForAndroid(base_url="http://phone:8080", username="u", password="p")
    assert gw.send("+1555", "x") is None


# --- HMAC verification (matches the documented algorithm) ---
def _sign(secret, body, ts):
    return hmac.new(secret.encode(), (body + ts).encode(), hashlib.sha256).hexdigest()

def test_verify_signature_roundtrip():
    body, ts, secret = '{"event":"sms:received"}', "1770000000", "s3cr3t"
    good = _sign(secret, body, ts)
    assert sms_receiver.verify_signature(secret, body, ts, good) is True
    assert sms_receiver.verify_signature(secret, body, ts, "deadbeef") is False

def test_verify_signature_disabled_without_key():
    assert sms_receiver.verify_signature("", "body", "1", "whatever") is True


# --- dedup ---
def test_dedup_flags_repeat():
    d = sms_receiver._Dedup(maxlen=10)
    assert d.seen("evt1") is False
    assert d.seen("evt1") is True
    assert d.seen("evt2") is False


# --- end-to-end receiver over a real socket ---
def _start(on_event, signing_key=""):
    httpd = sms_receiver.build_server(on_event=on_event, host="127.0.0.1", port=0,
                                      signing_key=signing_key)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]

def test_receiver_delivers_and_dedupes():
    got = []
    httpd, port = _start(got.append)
    try:
        body = json.dumps({"event": "sms:received", "id": "evt-1",
                           "payload": {"sender": "+15550001111", "message": "hi", "receivedAt": "t"}})
        url = f"http://127.0.0.1:{port}{config.SMS_WEBHOOK_PATH}"
        r = requests.post(url, data=body, headers={"Content-Type": "application/json"}, timeout=5)
        assert r.status_code == 200
        time.sleep(0.1)
        assert got and got[0]["payload"]["sender"] == "+15550001111"
        requests.post(url, data=body, timeout=5)   # duplicate id
        time.sleep(0.1)
        assert len(got) == 1                        # deduped
    finally:
        httpd.shutdown()

def test_receiver_rejects_bad_signature():
    got = []
    httpd, port = _start(got.append, signing_key="topsecret")
    try:
        body = json.dumps({"event": "sms:received", "id": "e2", "payload": {"sender": "+1", "message": "x"}})
        url = f"http://127.0.0.1:{port}{config.SMS_WEBHOOK_PATH}"
        # no signature -> 401, not delivered
        r = requests.post(url, data=body, timeout=5)
        assert r.status_code == 401
        # correct signature -> 200, delivered
        ts = str(int(time.time()))
        sig = _sign("topsecret", body, ts)
        r = requests.post(url, data=body, headers={"X-Signature": sig, "X-Timestamp": ts}, timeout=5)
        assert r.status_code == 200
        time.sleep(0.1)
        assert len(got) == 1
    finally:
        httpd.shutdown()


def test_send_retries_on_transient_failure(monkeypatch):
    """A briefly-asleep phone shouldn't surface as a hard SEND FAILED."""
    monkeypatch.setattr(sms_gateway.time, "sleep", lambda s: None)   # no real backoff
    calls = {"n": 0}
    def flaky_post(url, params=None, json=None, auth=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ConnectTimeout("phone asleep")
        return _Resp(202, {"id": "gw-ok"})
    monkeypatch.setattr(sms_gateway.requests, "post", flaky_post)
    gw = sms_gateway.SmsGateForAndroid(base_url="http://phone:8080", username="u", password="p")
    assert gw.send("+1555", "hello") == "gw-ok"      # recovered on the 3rd attempt
    assert calls["n"] == 3


def test_send_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(sms_gateway.time, "sleep", lambda s: None)
    def always_fail(url, params=None, json=None, auth=None, timeout=None):
        raise requests.exceptions.ConnectTimeout("unreachable")
    monkeypatch.setattr(sms_gateway.requests, "post", always_fail)
    gw = sms_gateway.SmsGateForAndroid(base_url="http://phone:8080", username="u", password="p")
    assert gw.send("+1555", "hello") is None