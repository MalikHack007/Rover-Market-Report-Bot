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


def topic_path() -> str:
    return f"projects/{GCP_PROJECT_ID}/topics/{PUBSUB_TOPIC}"


def subscription_path() -> str:
    return f"projects/{GCP_PROJECT_ID}/subscriptions/{PUBSUB_SUBSCRIPTION}"
