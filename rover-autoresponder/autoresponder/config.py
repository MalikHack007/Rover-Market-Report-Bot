"""Configuration, loaded from environment / .env.

Standing rule for this project: any entrypoint that needs secrets calls
load_dotenv() before reading os.environ.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# --- Google Cloud / Pub/Sub ---
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
PUBSUB_TOPIC = os.environ.get("PUBSUB_TOPIC", "rover-gmail")          # short name
PUBSUB_SUBSCRIPTION = os.environ.get("PUBSUB_SUBSCRIPTION", "rover-gmail-sub")

# --- Gmail (dedicated Rover-messages account) ---
GMAIL_CREDENTIALS_PATH = os.environ.get("GMAIL_CREDENTIALS_PATH", "./credentials.json")
GMAIL_TOKEN_PATH = os.environ.get("GMAIL_TOKEN_PATH", "./token.json")
# Addendum B: one token now covers Gmail (read) AND Calendar (write). Adding a scope
# invalidates the existing token, so token.json must be re-minted after this change.
# NOTE: the token acts as whichever account GRANTS consent — the Rover calendar must be
# created under that same account, not under whoever owns the Cloud project.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]
GMAIL_SCOPES = GOOGLE_SCOPES        # backward-compatible alias
WATCH_LABEL_IDS = ["INBOX"]

# --- Local state ---
DB_PATH = os.environ.get("DB_PATH", "./rover_autoresponder.db")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
HEARTBEAT_INTERVAL_SEC = int(os.environ.get("HEARTBEAT_INTERVAL_SEC", str(24 * 3600)))  # Phase 5 heartbeat
BOOT_ALERT_INTERVAL_SEC = int(os.environ.get("BOOT_ALERT_INTERVAL_SEC", "1800"))  # mute repeat boot-crash alerts

# --- Phase 3: Telegram delivery (send-only) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")   # from @BotFather
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")       # your numeric chat id

# --- Phase 2: LLM drafter (Anthropic) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")   # SDK also reads this
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
DRAFT_MAX_TOKENS = int(os.environ.get("DRAFT_MAX_TOKENS", "1024"))
SITTER_NAME = os.environ.get("SITTER_NAME", "")               # e.g. how clients address you
DEBOUNCE_SECONDS = int(os.environ.get("DEBOUNCE_SECONDS", "45"))  # coalesce a burst into 1 draft
_HERE = os.path.dirname(__file__)
PLAYBOOK_PATH = os.environ.get("PLAYBOOK_PATH", os.path.join(_HERE, "playbook.md"))
FAQ_PATH = os.environ.get("FAQ_PATH", os.path.join(_HERE, "faq.md"))  # optional; loaded if present


def topic_path() -> str:
    return f"projects/{GCP_PROJECT_ID}/topics/{PUBSUB_TOPIC}"


def subscription_path() -> str:
    return f"projects/{GCP_PROJECT_ID}/subscriptions/{PUBSUB_SUBSCRIPTION}"


# --- Addendum A / S1: SMS gateway (SMS Gateway for Android, local mode) ---
# Outbound: the box POSTs to the phone's on-device HTTP server.
SMS_GATEWAY_BASE_URL = os.environ.get("SMS_GATEWAY_BASE_URL", "http://192.168.1.10:8080")
SMS_GATEWAY_USERNAME = os.environ.get("SMS_GATEWAY_USERNAME", "")   # app Home tab creds
SMS_GATEWAY_PASSWORD = os.environ.get("SMS_GATEWAY_PASSWORD", "")
# LOCAL mode uses bare "/message"; CLOUD mode would be "/3rdparty/v1/messages".
SMS_GATEWAY_SEND_PATH = os.environ.get("SMS_GATEWAY_SEND_PATH", "/message")
# Retries for a briefly-unreachable phone (Wi-Fi power save / doze).
SMS_SEND_RETRIES = int(os.environ.get("SMS_SEND_RETRIES", "3"))
# Inbound: the phone POSTs webhooks to this receiver on the box.
SMS_WEBHOOK_HOST = os.environ.get("SMS_WEBHOOK_HOST", "0.0.0.0")
SMS_WEBHOOK_PORT = int(os.environ.get("SMS_WEBHOOK_PORT", "8899"))
SMS_WEBHOOK_PATH = os.environ.get("SMS_WEBHOOK_PATH", "/sms/webhook")
SMS_WEBHOOK_SIGNING_KEY = os.environ.get("SMS_WEBHOOK_SIGNING_KEY", "")  # Settings→Webhooks→Signing Key
SMS_WEBHOOK_CERT = os.environ.get("SMS_WEBHOOK_CERT", "")   # optional TLS (CA-issued for LAN IP)
SMS_WEBHOOK_KEY = os.environ.get("SMS_WEBHOOK_KEY", "")


# --- Returning clients (already booked before): skip the screening playbook ---
# {owner_name} and {pet_name} are filled from the thread. Editable without code changes.
RETURNING_CLIENT_TEMPLATE = os.environ.get(
    "RETURNING_CLIENT_TEMPLATE",
    "Hey {owner_name}, happy to take care of {pet_name} again, just accepted!",
)


# --- Addendum A / S6: cutover. SMS is primary; email is a FALLBACK-ONLY feed. ---
#   fallback  -> ingest + store only (feeds S5 truncation recovery). No drafting, no
#                Telegram, no button polling — so it can run alongside the SMS service
#                without fighting it for Telegram updates or double-replying.
#   standalone-> the original full email pipeline (pre-SMS behavior).
EMAIL_MODE = os.environ.get("EMAIL_MODE", "fallback")


def email_fallback_only() -> bool:
    return EMAIL_MODE.strip().lower() != "standalone"


# --- Addendum B: calendar ---
# The dedicated "Rover" calendar, created under the SAME account that grants OAuth
# consent (the Rover-messages account) — the token can only see that account's calendars.
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")
CALENDAR_TIMEZONE = os.environ.get("CALENDAR_TIMEZONE", "America/Chicago")
# Where a PENDING placeholder sits before the client picks a real time (30-min block,
# marked transparent so it never blocks the slots we're offering).
DEFAULT_DROPOFF_TIME = os.environ.get("DEFAULT_DROPOFF_TIME", "09:00")
DEFAULT_PICKUP_TIME = os.environ.get("DEFAULT_PICKUP_TIME", "17:00")
SLOT_MINUTES = int(os.environ.get("SLOT_MINUTES", "30"))


# --- Addendum B / C2: Cal.com scheduling links ---
CALCOM_USERNAME = os.environ.get("CALCOM_USERNAME", "")
CALCOM_BASE_URL = os.environ.get("CALCOM_BASE_URL", "https://cal.com")
CALCOM_EVENT_DROPOFF = os.environ.get("CALCOM_EVENT_DROPOFF", "dropoff")
CALCOM_EVENT_PICKUP = os.environ.get("CALCOM_EVENT_PICKUP", "pickup")
CALCOM_EVENT_MEETGREET = os.environ.get("CALCOM_EVENT_MEETGREET", "meet-greet")
CALCOM_API_KEY = os.environ.get("CALCOM_API_KEY", "")          # used by the C3 poller

# The message that carries the scheduling links. Fixed wording (no LLM call needed).
SCHEDULING_LINKS_TEMPLATE = os.environ.get(
    "SCHEDULING_LINKS_TEMPLATE",
    "Hey {owner_name}, just accepted — so excited to have {pet_name}! "
    "Please pick your times here:\n\n"
    "Drop-off ({start_date}): {dropoff_link}\n\n"
    "Pick-up ({end_date}): {pickup_link}\n\n"
    "Let me know if none of the times work and we'll sort something out!",
)
MEETGREET_LINK_TEMPLATE = os.environ.get(
    "MEETGREET_LINK_TEMPLATE",
    "Happy to do a meet and greet! Pick a time that works for you here: {meetgreet_link}",
)