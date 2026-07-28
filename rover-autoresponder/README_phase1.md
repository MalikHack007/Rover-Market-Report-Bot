# Rover Auto-Responder — Phase 1 (read side)

Proves the read pipeline end-to-end: **Gmail push → history.list → parse → SQLite (dedupe) → log**.
No LLM, no Telegram yet (those are Phases 2–4). Scope and decisions live in the design doc.

## What's here

```
autoresponder/
  config.py           env/.env config + topic/subscription path helpers
  models.py           ParsedMessage dataclass
  parser.py           Rover email -> ParsedMessage (subject + body only; ignores From)
  store.py            SQLite: dedupe, threads, messages, meta (history checkpoint)
  gmail_client.py     OAuth, watch(), history.list, get message, extract text
  pubsub_listener.py  Pub/Sub streaming pull (no public webhook needed)
  watch_renew.py      daily watch() renewal (it expires ~7 days out)
  main.py             live pipeline + offline --replay mode
tests/                parser + store tests (all offline, no creds)
samples/              vatsal_message.txt (from your real notification email)
```

## Try it offline right now (no credentials needed)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q                                   # 10 tests
python -m autoresponder.main --replay samples/vatsal_message.txt
sqlite3 rover_autoresponder.db "SELECT * FROM threads; SELECT * FROM messages;"
```

`--replay` runs parse → store → log against a local text file, so you can validate
parsing on real emails (save any notification as .txt with a leading `Subject:` line)
before the Gmail side is wired.

---

## What YOU need to set up (one time)

### 1. Dedicated Gmail account
Create a new Gmail just for Rover messages (e.g. `something.rover.bot@gmail.com`).
Do **not** reuse your primary / rover_report.py account.

### 2. Point Rover notifications at the dedicated account
Set Rover to deliver notification emails **straight to the dedicated address** — no
forwarding. The dedicated inbox then receives only Rover mail, direct from
`rover@e.rover.com`, so the watcher can process everything that arrives. The parser
keys off the **subject + body** (not the sender), so it stays robust regardless.

> If you did this by changing your Rover *login/account* email (rather than a
> notification-only setting), your existing Rover market-intel scraper session may
> need to re-auth under the new login.

### 3. Google Cloud project (Gmail push + Pub/Sub)

> **Two separate auth contexts** — don't conflate them:
> - **Gmail API** uses the dedicated account's *user OAuth* (`credentials.json` → `token.json`, scope `gmail.readonly`).
> - **Pub/Sub subscriber** uses a *service account* via `GOOGLE_APPLICATION_CREDENTIALS`
>   (a key with `roles/pubsub.subscriber`). The Gmail token does **not** authorize Pub/Sub.

In the [Google Cloud console](https://console.cloud.google.com):
1. Create (or reuse) a project; note the **project id** → `GCP_PROJECT_ID`.
2. Enable APIs: **Gmail API** and **Cloud Pub/Sub API**.
3. OAuth consent screen: external; add the dedicated Gmail as a user. To avoid the
   7-day refresh-token expiry, publish the app to **In production** (same as your
   report bot). Scope needed: `.../auth/gmail.readonly`.
4. Credentials → Create OAuth client ID → **Desktop app** → download as
   `credentials.json` into the repo root.
5. Pub/Sub → create a **topic** (e.g. `rover-gmail`) → `PUBSUB_TOPIC`.
6. On that topic, grant **Pub/Sub Publisher** to
   `gmail-api-push@system.gserviceaccount.com` (this is what lets Gmail publish).
7. Pub/Sub → create a **pull subscription** on the topic (e.g. `rover-gmail-sub`)
   → `PUBSUB_SUBSCRIPTION`.

### 4. Fill in `.env`
```bash
cp .env.example .env      # then edit values from steps above
```

### 5. First-run OAuth (produces token.json)
Run the live entrypoint once **on the box that will host it**, signed into the
dedicated account when the browser opens:
```bash
python -m autoresponder.main
```
This creates `token.json`, registers `watch()`, and starts listening. Send a test
message on Rover and watch the log for a `NEW MSG | ...` line.

> `credentials.json`, `token.json`, `.env`, and `*.db` are gitignored. If any secret
> is ever committed, rotate it.

---

## What I've verified vs. what needs your live run
- **Verified here (offline):** parser against your real Vatsal sample (owner/pet/dates/
  message, curly-apostrophe subjects, multi-line messages, HTML fallback, unrecognized
  formats flagged not crashed) and the SQLite dedupe/thread/meta logic — 10 passing tests.
- **Needs your run:** the Gmail OAuth + `watch()` + Pub/Sub delivery, since that needs
  the dedicated account and GCP project. If a booking *request* email (vs a *message*)
  has a different layout, save it via `--replay`; unrecognized formats are logged, and
  I'll add a parser variant from the real sample.

Next up (Phase 2): the conversation state machine + LLM drafter.
