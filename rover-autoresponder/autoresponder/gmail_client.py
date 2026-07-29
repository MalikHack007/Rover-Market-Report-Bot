"""Gmail API access for the dedicated Rover-messages account.

Not exercised by the offline tests (needs real OAuth). Auth uses an installed-app
flow the first time (produces token.json), then refreshes silently.
"""
import logging
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError  # Phase 3 fix: catch 404s from get_message

from . import config
from .parser import extract_text_from_payload

log = logging.getLogger(__name__)


def get_credentials() -> Credentials:
    creds = None
    if os.path.exists(config.GMAIL_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(
            config.GMAIL_TOKEN_PATH, config.GMAIL_SCOPES
        )
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GMAIL_CREDENTIALS_PATH, config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(config.GMAIL_TOKEN_PATH, "w") as fh:
            fh.write(creds.to_json())
    return creds


def build_service(creds: Credentials = None):
    creds = creds or get_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def start_watch(service) -> dict:
    """(Re)register Gmail push to the Pub/Sub topic. Returns {historyId, expiration}."""
    body = {"labelIds": config.WATCH_LABEL_IDS, "topicName": config.topic_path()}
    return service.users().watch(userId="me", body=body).execute()


def list_history(service, start_history_id: str):
    """Return message ids added since start_history_id (deduped, in order)."""
    msg_ids, page_token = [], None
    while True:
        resp = (
            service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",  # Phase 3 fix: match the INBOX watch; drop non-inbox phantoms
                pageToken=page_token,
            )
            .execute()
        )
        for h in resp.get("history", []):
            for added in h.get("messagesAdded", []):
                msg_ids.append(added["message"]["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    seen, out = set(), []
    for m in msg_ids:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def get_message(service, msg_id: str):
    """Fetch a full message, or return None if it's gone (404).

    Phase 3 fix: history.list can reference a message that has since been deleted
    or moved; fetching it 404s. That's expected in Gmail sync — skip it, don't crash.
    """
    try:
        return service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()
    except HttpError as e:
        if getattr(e, "resp", None) is not None and e.resp.status == 404:
            log.info("message %s not found (deleted/moved); skipping", msg_id)
            return None
        raise


def extract_fields(msg: dict):
    """Return (subject, body_text, thread_id) from a full Gmail message."""
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    subject = headers.get("subject", "")
    thread_id = msg.get("threadId", "")
    body_text = extract_text_from_payload(payload)
    return subject, body_text, thread_id