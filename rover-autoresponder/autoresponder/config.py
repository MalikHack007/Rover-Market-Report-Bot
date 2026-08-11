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
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
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