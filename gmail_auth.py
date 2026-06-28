"""
gmail_auth.py - one-time Gmail authorization for rover_report.py.

Run this ONCE, on the box that will run cron. It opens a browser; you sign in and
grant gmail.send. It writes token_send.json (which holds the refresh token) next
to this file, locked to owner-only. After this, rover_report.py runs unattended.

    python gmail_auth.py

Prereq: credentials.json (Desktop OAuth client) sits next to this script, and the
OAuth consent screen is published to "In production" (otherwise the refresh token
expires in 7 days).
"""

import os
import stat
from google_auth_oauthlib.flow import InstalledAppFlow

HERE = os.path.dirname(os.path.abspath(__file__))
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
CRED = os.path.join(HERE, "credentials.json")
TOKEN = os.path.join(HERE, "token_send.json")


def main():
    if not os.path.exists(CRED):
        raise SystemExit("Put credentials.json (Desktop OAuth client) next to this script first.")

    flow = InstalledAppFlow.from_client_secrets_file(CRED, SCOPES)
    # access_type=offline + prompt=consent GUARANTEES a refresh token comes back.
    # Without prompt=consent, Google only returns a refresh token on the very first
    # consent ever, so a re-auth could silently hand you a token with no refresh.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    with open(TOKEN, "w") as f:
        f.write(creds.to_json())
    os.chmod(TOKEN, stat.S_IRUSR | stat.S_IWUSR)  # 0600: owner read/write only

    print(f"\nWrote {TOKEN}")
    print(f"refresh token present: {bool(creds.refresh_token)}")
    if not creds.refresh_token:
        print("NO refresh token -- delete token_send.json and re-run.")


if __name__ == "__main__":
    main()
