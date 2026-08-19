"""Addendum A / S1 — outbound SMS via the phone gateway.

Concrete adapter for SMS Gateway for Android (capcom6, sms-gate.app) in LOCAL mode:
the box POSTs to the phone's on-device HTTP server. Kept behind the SmsGateway
interface so the mechanism (gateway app / Tasker / ADB) can be swapped without
touching callers.

Send API (LOCAL mode — bare paths, no /3rdparty/v1 prefix; that's cloud-only):
  POST {phone}/message                (HTTP Basic auth from the app's Home tab)
  body: {"textMessage": {"text": ...}, "phoneNumbers": [number], ...}
We pass ?skipPhoneValidation=true because Rover's per-conversation relay numbers
may not be clean E.164.
"""
import logging
import time

import requests
from requests.auth import HTTPBasicAuth

from . import config

log = logging.getLogger(__name__)


class SmsGateway:
    """Swappable transport interface. Implementations must not raise on send failure."""

    def send(self, number: str, text: str, message_id: str = None):
        raise NotImplementedError


class SmsGateForAndroid(SmsGateway):
    def __init__(self, base_url=None, username=None, password=None):
        self.base = (base_url or config.SMS_GATEWAY_BASE_URL).rstrip("/")
        self.auth = HTTPBasicAuth(
            username or config.SMS_GATEWAY_USERNAME,
            password or config.SMS_GATEWAY_PASSWORD,
        )

    def send(self, number: str, text: str, message_id: str = None):
        """Queue an SMS on the phone. Returns the gateway message id, or None on failure.

        LOCAL mode uses bare paths (POST /message). The /3rdparty/v1 prefix is
        CLOUD mode only — using it against a local server returns 404.

        message_id (optional) sets the gateway-side id for idempotency (used in S4 so a
        retry/double-approve can't double-send).
        """
        payload = {
            "textMessage": {"text": text},
            "phoneNumbers": [number],
        }
        if message_id:
            payload["id"] = message_id
        url = f"{self.base}{config.SMS_GATEWAY_SEND_PATH}"
        # The phone can be briefly unreachable (Wi-Fi power save / doze), which showed
        # up as a hard "SEND FAILED" on the first tap and success on a retry. Retry a
        # few times with backoff, and keep the connect timeout short so a dead attempt
        # is abandoned quickly rather than blowing past Telegram's callback deadline.
        last_error = None
        for attempt in range(1, config.SMS_SEND_RETRIES + 1):
            try:
                r = requests.post(
                    url,
                    params={"skipPhoneValidation": "true"},
                    json=payload,
                    auth=self.auth,
                    timeout=(5, 15),      # (connect, read)
                )
            except Exception as e:
                last_error = e
                log.warning("SMS send attempt %d/%d to %s failed: %s",
                            attempt, config.SMS_SEND_RETRIES, number, type(e).__name__)
                if attempt < config.SMS_SEND_RETRIES:
                    time.sleep(2 * attempt)      # 2s, 4s, ...
                continue
            if r.status_code in (200, 201, 202):
                try:
                    return r.json().get("id")
                except Exception:
                    return None
            log.error("SMS send failed: %s %s", r.status_code, r.text[:300])
            return None       # a real HTTP rejection won't fix itself on retry
        log.error("SMS send to %s failed after %d attempts: %s",
                  number, config.SMS_SEND_RETRIES, last_error)
        return None