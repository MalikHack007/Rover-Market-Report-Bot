# CLAUDE.md — rover-automations

Project memory for Claude Code. Read this first, then read the actual tree — the design
docs describe *intended* design and in a few places the built code has moved past them.
**When a design doc and the code disagree, the code is the truth**; note the drift to Malik.

**Owner:** Malik (`malikhack007`)  ·  **Host:** Xubuntu Linux in a **VirtualBox VM (bridged
networking)**  ·  **Python:** 3.10 (venv)

The auto-responder subsystem has its **own memory file** at
`rover-autoresponder/CLAUDE.md` — read that too before working in that directory.

---

## What this repo is

Two independent automations for a Rover dog-boarding business, sharing an environment and
conventions but running as separate processes.

1. **Daily Rank & Pricing Bot** — *(repo root)* — a once-a-day job that checks the
   operator's search rank and the local median boarding price across five Austin zips and
   emails a report. Fire-and-exit.
2. **New-Client Auto-Responder + Calendar/Scheduling** — *(`rover-autoresponder/`)* — a set
   of long-running services that read new-client messages over **SMS** (with email as a
   load-bearing secondary channel — see its CLAUDE.md), draft on-brand replies, send them
   after your one-tap approval, and manage
   Google Calendar / Cal.com booking events. **Details in `rover-autoresponder/CLAUDE.md`.**

---

## Repo layout (real tree, trimmed)

```
rover-automations/
├── rover_report.py            ← subsystem 1: the rank/price bot
├── gmail_auth.py              ← subsystem 1: one-time Gmail OAuth helper
├── rover-report.service       ← subsystem 1 runs via a systemd TIMER (not cron)
├── rover-report.timer
├── report_log.csv             ← rank/price history        [keep out of git]
├── credentials.json           ← Google OAuth client        [SECRET — do not commit]
├── token_send.json            ← cached gmail.send token     [SECRET — do not commit]
├── shot_787??.png             ← per-zip verification screenshots (all 5 present)
├── rover_bot_design.md        ← subsystem 1 design doc
└── rover-autoresponder/       ← subsystem 2 — SEE ITS OWN CLAUDE.md
    ├── autoresponder/         ← the actual Python package (all *.py live here)
    ├── tests/                 ← pytest suite
    ├── systemd/               ← three user services (see nested CLAUDE.md)
    ├── samples/               ← real Rover message samples for parser tests
    ├── certs/                 ← webhook.crt / webhook.key (TLS for the inbound SMS webhook)
    ├── requirements.txt
    ├── rover_autoresponder.db ← SQLite state              [do not commit]
    ├── credentials.json, token.json, pubsub-sa.json        [SECRETS — do not commit]
    └── design docs: rover_autoresponder_design.md,
        rover_autoresponder_design_addendum_A.md  (SMS transport),
        rover_calendar_design_addendum_B.md       (calendar, v0.6)
```

Confirm real module names by reading the tree/files; don't infer them from the design docs.

---

## Hard rules (do not violate)

- **No unattended sending — ever.** Every outbound client message goes out only after an
  explicit human **Approve** tap in Telegram. There is **no auto-send path and none may be
  added**. The approval gate is the safety keystone: keep it idempotent (no double-send),
  un-bypassable, target-labeled (the card names the client), and delivery-confirmed. Full
  detail in `rover-autoresponder/CLAUDE.md`.
- **Don't automate Rover's web platform.** No posting/booking/scraping through the Rover
  website or app. The auto-responder reads **SMS + your own email**, never the Rover web
  surface; the rank bot only *reads* public page-1 search results.
- **No anti-bot circumvention.** No CAPTCHA solvers, stealth tooling, or proxies. If
  Cloudflare challenges the rank bot: wider spacing / fewer zips, and report the failure —
  never escalate.
- **`load_dotenv()` at the top of every entrypoint.** Standing rule.
- **Absolute paths everywhere.** systemd/timer units run with a minimal env; relative paths
  break silently.
- **Secrets never get committed.** `credentials.json` (both copies), `token_send.json`,
  `token.json`, `pubsub-sa.json`, `certs/*.key`, `.env`, the `.db`, `report_log.csv`, and
  the real `playbook.md` / `faq.md` all stay out of git (only their `.example` copies are
  committed). If a secret ever lands in a commit, **rotate it**. Verify `.gitignore` covers
  every item above before committing.
- **Design-doc-before-code.** Non-trivial changes get a design/plan pass first.

---

## Environment & running

- Python **3.10** in a venv. Subsystem 2 has its own venv under `rover-autoresponder/`.
- Runs on **Xubuntu inside a bridged VirtualBox VM**. The bridged VM's broken IPv6 is why
  the auto-responder force-prefers IPv4 at startup — see the nested CLAUDE.md; don't undo it.
- **Subsystem 1** runs on a **systemd timer** (`rover-report.service` + `.timer`) — the
  design doc still says cron; the timer is the real mechanism. Fire-and-exit, ~08:00 CDT.
  Confirm the schedule from the `.timer` file. First run is manual to complete the one-time
  `gmail.send` OAuth consent (`gmail_auth.py`).
- **Subsystem 2** runs as **three systemd *user* services** with **linger** enabled
  (`loginctl enable-linger malikhack007`) so they survive logout. See its CLAUDE.md.
- **Tests:** from `rover-autoresponder/`, `python -m pytest` (suite under `tests/`).
  Install deps with `pip install -r rover-autoresponder/requirements.txt`.

---

## Subsystem 1 — Daily Rank & Pricing Bot  *(root)*

**Code:** `rover_report.py`  ·  **Auth helper:** `gmail_auth.py`  ·  **Design:** `rover_bot_design.md`

- Playwright (headless Chromium) loads **page 1 only** of an overnight-boarding search for
  **today → tomorrow**, for five fixed Austin zips: `78753, 78723, 78701, 78757, 78751`.
- Per-zip search URLs are **captured once and frozen as templates**; at runtime only
  `start_date`, `end_date`, `page` are rewritten (no geocoding — this is why the zip set is
  fixed). Adding a zip = pasting one captured URL into config.
- Parsing keys on the text anchor **"per night"** (card CSS classes are hashed/unstable),
  excluding `<script>`/`<style>`. Rank = 1-based position of the first card containing
  `MY_SITTER_NAME`, else the literal `NOT ON FIRST PAGE`. Rank reflects *displayed* order
  (includes sponsored placements).
- Aggregates: median per zip + an **overall median pooled across all zips' rates** (not an
  average of medians).
- Output: HTML+text email via **Gmail API**, scope **`gmail.send` only**
  (`credentials.json` → `token_send.json`). History appended to `report_log.csv`
  (`date, zip, rank, median, n_sitters`, plus an `AGGREGATE` row).
- Screenshots: the design says "on failure only," but `shot_787??.png` for all five zips
  are currently in root — reconcile the retention behavior with the code before relying on it.
- Config is in-module constants: `MY_SITTER_NAME`, `EMAIL_TO`, `ZIP_URLS`, `DELAY`, `HEADLESS`.
- Fault isolation: one zip failing (empty page / Cloudflare) is recorded and the run
  continues; email-send failure writes the report to the log so data isn't lost.

---

## Secrets & credentials map

| File | Belongs to | Scope / purpose |
|---|---|---|
| `credentials.json` (root) | Subsystem 1 | Google OAuth client, `gmail.send` |
| `token_send.json` (root) | Subsystem 1 | cached send token |
| `rover-autoresponder/credentials.json` | Subsystem 2 | **dedicated** Gmail account (separate from subsystem 1) |
| `rover-autoresponder/token.json` | Subsystem 2 | cached token for that account |
| `rover-autoresponder/pubsub-sa.json` | Subsystem 2 | Pub/Sub service account (Gmail push) |
| `rover-autoresponder/.env` | Subsystem 2 | Anthropic / Telegram / Cal.com keys, paths |
| `rover-autoresponder/certs/webhook.*` | Subsystem 2 | TLS for the LAN HTTPS endpoint the phone SMS gateway POSTs inbound texts to |

All of the above are gitignore targets. The two `credentials.json` files are **different
accounts** — keep them straight.

---

## Design docs & canonicity

- `rover_bot_design.md` — canonical for subsystem 1 (verify vs code: cron→timer, screenshots).
- `rover_autoresponder_design.md` (v0.3) — subsystem-2 **base "brain"** (drafter, playbook,
  FAQ, stage machine, debounce, SQLite) is still valid, **but its transport is superseded**:
  the read/deliver model is no longer Gmail-push + Telegram-tap-to-copy.
- `rover_autoresponder_design_addendum_A.md` — **Addendum A (SMS transport & approve-and-send)**
  — canonical for how messages are read, identified, and sent.
- `rover_calendar_design_addendum_B.md` (v0.6) — canonical for the calendar/scheduling layer.