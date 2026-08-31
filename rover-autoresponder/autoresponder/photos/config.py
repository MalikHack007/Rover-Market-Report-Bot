"""Config for the photo-update feature. Reads env; importing the parent `config` ensures
load_dotenv() has already run (standing project rule)."""
import os

from .. import config as _parent  # noqa: F401 — imported for its load_dotenv() side effect

# --- Telerivet (outbound MMS, send-only) ---
TELERIVET_API_KEY = os.environ.get("TELERIVET_API_KEY", "")
TELERIVET_PROJECT_ID = os.environ.get("TELERIVET_PROJECT_ID", "")
TELERIVET_PHONE_ID = os.environ.get("TELERIVET_PHONE_ID", "")   # which phone/number sends
TELERIVET_API_BASE = os.environ.get("TELERIVET_API_BASE", "https://api.telerivet.com/v1")

# --- Daily budgets (Telerivet plan) ---
TELERIVET_DAILY_MSG_CAP = int(os.environ.get("TELERIVET_DAILY_MSG_CAP", "50"))
TELERIVET_DAILY_API_CAP = int(os.environ.get("TELERIVET_DAILY_API_CAP", "200"))
# Poll calls stop once they'd cross this, leaving the rest of the API cap reserved for sends
# (so sends always win the budget and 200/day is never hit). 140 reserves ~60 for sends.
TELERIVET_DAILY_POLL_BUDGET = int(os.environ.get("TELERIVET_DAILY_POLL_BUDGET", "140"))
# Delivery-status poller (P2): how often to poll while a batch is settling, and when to stop
# chasing a straggler (MMS receipts are flaky) and mark it "sent (unconfirmed)".
PHOTO_POLL_INTERVAL_SEC = int(os.environ.get("PHOTO_POLL_INTERVAL_SEC", "60"))
PHOTO_GIVE_UP_MINUTES = int(os.environ.get("PHOTO_GIVE_UP_MINUTES", "30"))

# --- Cloudflare R2 (media hosting) ---
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ENDPOINT_URL = os.environ.get("R2_ENDPOINT_URL", "")   # or built from R2_ACCOUNT_ID
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "")
R2_PRESIGN_TTL = int(os.environ.get("R2_PRESIGN_TTL", "3600"))
# Photos are sent at ORIGINAL SIZE; this quality is used ONLY when a rotated photo must be
# re-encoded to bake in EXIF orientation (upright photos are uploaded untouched).
R2_IMAGE_QUALITY = int(os.environ.get("R2_IMAGE_QUALITY", "95"))

# --- Captions (pre-written pool, no LLM) ---
_HERE = os.path.dirname(__file__)
CAPTIONS_PATH = os.environ.get("CAPTIONS_PATH", os.path.join(_HERE, "captions.txt"))
