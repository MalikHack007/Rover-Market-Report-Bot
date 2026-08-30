"""Telerivet client — outbound MMS (send-only) + delivery-status queries.

Sends from Malik's own number via the Pixel gateway (proven; see ../../telerivet_poc.py).
Telerivet is configured send-only (incoming forwarding OFF) so inbound doesn't consume the
50/day quota — inbound is owned by "SMS Gateway for Android".

Delivery status is polled in BATCHES: `query_messages(status="queued")` returns every pending
message in ONE API call — do NOT call get_message() per message in the poller (that's what
blows the 200/day budget).
"""
import logging
import os
from urllib.parse import urlparse

import requests

from . import config

log = logging.getLogger(__name__)

# Telerivet message statuses: queued | sent | failed | delivered | not_delivered | cancelled
DELIVERED = {"delivered"}
FAILED = {"failed", "not_delivered", "cancelled", "canceled"}
TERMINAL = DELIVERED | FAILED


def _media_item(url):
    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
    return {"url": url, "type": mime, "filename": os.path.basename(path) or "photo.jpg"}


class TelerivetClient:
    def __init__(self, api_key=None, project_id=None, phone_id=None, base=None):
        self.api_key = api_key or config.TELERIVET_API_KEY
        self.project_id = project_id or config.TELERIVET_PROJECT_ID
        self.phone_id = phone_id or config.TELERIVET_PHONE_ID
        self.base = base or config.TELERIVET_API_BASE

    def _url(self, suffix=""):
        return f"{self.base}/projects/{self.project_id}/messages{suffix}"

    def send(self, to_number, content, media_urls=None):
        """Send ONE MMS (media_urls = list of public URLs) or an SMS. One dog-update = one call.

        Returns {"id", "status"}. Raises requests.HTTPError on a non-2xx (caller handles).
        """
        payload = {"to_number": to_number, "content": content}
        if self.phone_id:
            payload["phone_id"] = self.phone_id
        if media_urls:
            payload["media"] = [_media_item(u) for u in media_urls]
        r = requests.post(self._url("/send"), auth=(self.api_key, ""), json=payload,
                          timeout=(5, 30))
        r.raise_for_status()
        m = r.json()
        return {"id": m.get("id"), "status": (m.get("status") or "").lower()}

    def get_message(self, message_id):
        """Status of ONE message (GET /messages/{id}). Prefer query_messages() in the poller."""
        r = requests.get(self._url(f"/{message_id}"), auth=(self.api_key, ""), timeout=(5, 30))
        r.raise_for_status()
        m = r.json()
        return {"id": m.get("id"), "status": (m.get("status") or "").lower(),
                "error_message": m.get("error_message")}

    def query_messages(self, **params):
        """Batched status poll: GET /messages with filters (e.g. status='queued',
        direction='outgoing'). ONE API call returns many messages. Returns [{"id","status"}].

        With the 50/day message cap everything outstanding fits on one page; if Telerivet
        paginates (`next_id`), the poller can page, but at this volume it won't need to.
        """
        r = requests.get(self._url(), auth=(self.api_key, ""), params=params, timeout=(5, 30))
        r.raise_for_status()
        data = r.json()
        rows = data.get("data") if isinstance(data, dict) else data
        return [{"id": m.get("id"), "status": (m.get("status") or "").lower()}
                for m in (rows or [])]
