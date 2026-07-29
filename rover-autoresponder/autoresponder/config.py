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