"""Global test isolation.

The real .env is loaded at import (config.load_dotenv()), so during tests the live
TELEGRAM / GOOGLE_CALENDAR / CAL.COM credentials are all active. Without a guard, any
code path that reaches an un-stubbed send — e.g. handle_sms() on a `confirmed` marker,
which calls on_booking_confirmed() + send_scheduling_links() — fires REAL Telegram cards
and writes REAL calendar events. This autouse fixture severs those outbound side effects
for the ENTIRE suite, so a test can never message the bot or touch the calendar again.

Everything is disabled through the app's OWN guards rather than by patching internals:
`telegram_notify._call` already no-ops when the token/chat are unset, and the confirmed
branch of handle_sms is gated on `GOOGLE_CALENDAR_ID`. Blanking those config values here
disables the live paths for every test, while a test that specifically exercises one of
them just sets its own value (and stubs the transport), which overrides this fixture.
"""
import pytest

from autoresponder import config


@pytest.fixture(autouse=True)
def _no_outbound(monkeypatch):
    # Telegram: enabled() is False with no token/chat, so _call returns before any HTTP —
    # blocks send_message / send_draft_card / send_alert / edit_* / answer_callback.
    # test_telegram sets its own token + stubs _SESSION.post, so it still tests the real path.
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    # Calendar / Cal.com: the confirmed branch of handle_sms is gated on GOOGLE_CALENDAR_ID,
    # so blanking it stops on_booking_confirmed() from reaching the real Google Calendar.
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_ID", "")
    monkeypatch.setattr(config, "CALCOM_API_KEY", "")
