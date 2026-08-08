"""Addendum A / S1 — inbound SMS webhook receiver (ingest + log only).

Receives SMS Gateway for Android webhooks (sms:received + delivery events) over a
plain stdlib HTTP server, verifies the HMAC signature, dedupes on the event id
(the app retries until it gets a 2xx, so duplicates are expected), and hands each
event to a callback. S1 just logs; S2+ will route sms:received into the pipeline.

Signature (per docs): X-Signature = hex HMAC-SHA256(secret, raw_body + X-Timestamp).
"""
import hashlib
import hmac
import json
import logging
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config

log = logging.getLogger(__name__)


def verify_signature(secret_key: str, raw_body: str, timestamp: str, signature: str) -> bool:
    """Constant-time HMAC check. If no key is configured, verification is disabled."""
    if not secret_key:
        return True
    message = (raw_body + (timestamp or "")).encode()
    expected = hmac.new(secret_key.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or "").strip().lower())


class _Dedup:
    """Bounded in-memory set of seen event ids (S1). S2 persists this properly."""

    def __init__(self, maxlen: int = 4000):
        self.maxlen = maxlen
        self._order = deque()
        self._seen = set()

    def seen(self, key: str) -> bool:
        if not key:
            return False
        if key in self._seen:
            return True
        self._seen.add(key)
        self._order.append(key)
        while len(self._order) > self.maxlen:
            self._seen.discard(self._order.popleft())
        return False


def log_event(data: dict) -> None:
    event = data.get("event")
    p = data.get("payload") or {}
    if event == "sms:received":
        log.info("INBOUND SMS | from=%s at=%s | %r",
                 p.get("sender"), p.get("receivedAt"), p.get("message"))
    elif event in ("sms:sent", "sms:delivered", "sms:failed", "sms:cancelled"):
        log.info("SEND STATUS | %s | to=%s msgId=%s %s",
                 event, p.get("recipient"), p.get("messageId"), p.get("reason") or "")
    else:
        log.info("SMS EVENT | %s | %s", event, p)


def make_handler(on_event, dedup, signing_key, path, max_skew=300):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence stdlib's per-request stderr logging
            pass

        def do_POST(self):
            if path and self.path.split("?")[0].rstrip("/") != path.rstrip("/"):
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace")
            sig = self.headers.get("X-Signature", "")
            ts = self.headers.get("X-Timestamp", "")

            if signing_key:
                if not verify_signature(signing_key, raw, ts, sig):
                    log.warning("SMS webhook: bad signature")
                    self.send_response(401)
                    self.end_headers()
                    return
                if ts and abs(time.time() - int(ts)) > max_skew:
                    log.warning("SMS webhook: stale timestamp (replay?)")
                    self.send_response(401)
                    self.end_headers()
                    return

            # Ack fast — the app retries anything that isn't 2xx within 30s.
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

            try:
                data = json.loads(raw)
            except Exception:
                log.warning("SMS webhook: unparseable body")
                return
            if dedup.seen(data.get("id")):
                return  # retry of an event we already handled
            try:
                on_event(data)
            except Exception:
                log.exception("SMS webhook handler error")

    return Handler


def build_server(on_event=None, host=None, port=None, signing_key=None):
    on_event = on_event if on_event is not None else log_event
    host = host if host is not None else config.SMS_WEBHOOK_HOST
    port = port if port is not None else config.SMS_WEBHOOK_PORT
    signing_key = signing_key if signing_key is not None else config.SMS_WEBHOOK_SIGNING_KEY

    handler = make_handler(on_event, _Dedup(), signing_key, config.SMS_WEBHOOK_PATH)
    httpd = ThreadingHTTPServer((host, port), handler)

    # Optional TLS: the gateway app requires HTTPS for non-127.0.0.1 webhook targets
    # (use its Certificate Authority to issue a cert for the box's LAN IP).
    if config.SMS_WEBHOOK_CERT and config.SMS_WEBHOOK_KEY:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(config.SMS_WEBHOOK_CERT, config.SMS_WEBHOOK_KEY)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    return httpd


def serve(on_event=None) -> None:
    httpd = build_server(on_event=on_event)
    scheme = "https" if (config.SMS_WEBHOOK_CERT and config.SMS_WEBHOOK_KEY) else "http"
    host, port = httpd.server_address[0], httpd.server_address[1]
    log.info("SMS webhook receiver on %s://%s:%s%s", scheme, host, port, config.SMS_WEBHOOK_PATH)
    httpd.serve_forever()
