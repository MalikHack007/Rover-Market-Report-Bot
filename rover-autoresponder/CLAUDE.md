# CLAUDE.md — rover-autoresponder

Memory for the auto-responder + calendar subsystem. Read the **root `CLAUDE.md`** first for
shared conventions and hard rules. Code in `autoresponder/` is the truth; the design docs
(`rover_autoresponder_design.md` v0.3 + Addendum A (SMS) + Addendum B v0.6 (calendar)) are
the intent and are partly behind the code.

---

## What this does

Reads each new-client Rover message **over SMS**, figures out which screening stage the
conversation is in, drafts a reply in Malik's voice, shows it in Telegram, and — **only
after an explicit Approve tap** — sends the reply back over SMS. On booking confirmation it
places PENDING Google Calendar events and reconciles them as clients book via Cal.com.

---

## Transport reality

Per **Addendum A**, the system is **SMS-primary, email-fallback**, in **approve-and-send** mode:

- **Read = SMS via a phone gateway.** A **Pixel 9a** runs the **"SMS Gateway for Android"**
  app in **local mode**: it POSTs each inbound text to the box over **HTTPS on the LAN**
  (that's what `certs/webhook.*` secures) and exposes a send-to-number endpoint. **No
  third-party cloud relay** — messages flow only through the phone and the box. The brain
  lives entirely on the box; the phone is a dumb modem.
- **Identity = the conversation's phone number.** Rover assigns a unique number per request
  that persists across the whole lifecycle, so the **number is the stable primary key**.
  Numbers are **reused across a client's future bookings**, so bookings are separated by an
  **episode** system rather than by number alone.
- **Send = the bot sends over SMS**, after approval. The copy-paste step is gone.
- **Email = a load-bearing secondary channel, not a backup.** It runs ingest-only
  (`EMAIL_MODE=fallback`), but the system genuinely depends on it — see "Email pipeline"
  below. In short: it supplies authoritative dates-with-year and the phone↔thread binding at
  confirmation, carries **all** booking modifications (SMS never does), and recovers truncated
  long messages.

**Classifier = in-thread SMS marker state machine** (SMS has no subject line):
- `"New booking request (…) from … Book @ r.rover.com/…"` → **opens** an ACTIVE INQUIRY
  (multi-turn drafting). Owner/pet/age/weight/dates/booking-URL are parsed straight from this
  inline block — no LLM inference for those.
- `"… wants you to care for … Confirm booking ASAP"` → **pay-first, awaiting-acceptance**
  signal (client has paid; you haven't accepted yet).
- `"… has confirmed a booking request …"` → **CONVERTED** (stop drafting).
- `"Your upcoming booking … has been modified …"` → modification.
- Ordinary confirmed-conversation texts carry **no markers**, so "no marker + already
  converted" stays quiet. Confirmed threads **stop drafting entirely** except for the
  scheduling-links card.

The "**S4**" the calendar addendum references is Addendum A's **approve-and-send phase**, not
a fifth screening stage.

---

## Email pipeline — load-bearing, not a backup

The `EMAIL_MODE=fallback` label names the email service's *operating mode* (ingest-only: no
drafting, no Telegram poll, so it doesn't fight the SMS service's poller) — **not** its
importance. SMS is the primary transport, but the system depends on email for four things,
three of which SMS cannot provide:

1. **Authoritative dates — with the year.** SMS gives dates without a year (`09/01 to 09/06`).
   The **confirmation email carries full dates including the year in the subject**
   (`Confirmed: Mazzy's upcoming booking from Aug 20, 2026 - Aug 23, 2026`) and body. Calendar
   events use the email's dates; SMS-first events are corrected once the email confirms. This
   is what removes the year-inference guesswork entirely.
2. **The phone ↔ thread binding.** The confirmation email **body carries the client's phone
   number** (`Phone number: (310) 307-3340`) alongside owner and pet names, so the SMS number
   binds to the email conversation thread **deterministically** — no fuzzy name matching.
3. **All booking modifications.** Date changes arrive **only** by email (the "revised
   itinerary" message, e.g. `Dates: Aug 5, 2026 - Aug 10, 2026`) — **never by SMS**. Without
   the email pipeline the calendar silently goes stale on every reschedule. These arrive as a
   **separate standalone email** (not inside the conversation thread), correlated by a dual
   strategy: primary **owner + pet match** (both names required, normalized, restricted to
   currently-upcoming bookings; ambiguous or zero matches → **alert, never guess**) plus a
   **date-drift** safety net on ordinary message notifications.
4. **Truncation recovery** (its original job): recovering the full text of a long client
   message that SMS cut off at the cookie-gated `more at …` link, correlated to the SMS number
   once per request.

**Ordering:** whichever of SMS/email arrives first creates the events; the other enriches. So
"fallback" describes when it runs, not how much rides on it — treat the email path as
correctness-critical, especially for calendar dates.

---

## Module map (`autoresponder/*.py`)

**SMS transport & approve-and-send**
- `sms_gateway.py` — talks to the Pixel 9a gateway; `send(number, text) → delivery_callback`.
- `sms_receiver.py` — inbound SMS HTTPS webhook endpoint (box side of the bridge).
- `sms_parser.py` — the marker state machine + booking-request block parsing.
- `sms_approve.py` — Approve/Edit → send; idempotency + delivery confirmation.
- `sms_pipeline.py` / `sms_main.py` — SMS pipeline wiring / entrypoint.
- `truncation.py` — detect the `more at …` tail, drive email-fallback recovery.
- `identity.py` — number keying + episode logic + email-thread correlation.
- `netprefs.py` — **process-level `socket.getaddrinfo` patch to prefer IPv4.** Fixes ~16s
  stalls from the bridged VM's broken IPv6 (hit Telegram callbacks, Cal.com polls, Calendar
  writes). Must be imported **early**, before networking starts. **Do not remove.**

**The brain (reused from v0.3)**
- `drafter.py` — Anthropic API (Claude Sonnet drafter, optional Haiku classifier); few-shot
  from `playbook.md`; JSON out `{stage, draft_text, flags[], off_playbook}`.
- `playbook.md` (+ `.example`) — screening playbook / few-shot source of truth. Uses
  `{SITTER_ALIASES}` and `{WRONG_SITTER_TEMPLATE}` placeholders substituted at prompt-build.
- `faq.md` (+ `.example`) — **present now** (was deferred); loaded if it exists, for S3
  ad-hoc questions.
- `debounce.py` — coalesces multi-text openers (stragglers like "I forgot to add my kitten").
- `store.py` / `models.py` — SQLite state store + schema.

**Email side (now fallback)**
- `gmail_client.py`, `pubsub_listener.py`, `watch_renew.py` — Gmail read, Pub/Sub push,
  daily `watch()` renewal. Post-cutover this is truncation-recovery + confirmation email,
  not the primary trigger.
- `confirmation_email.py` — parses the confirmation email (client phone number + full dates
  with year) to bind number ↔ email thread and supply authoritative dates.

**Calendar / scheduling (Addendum B v0.6)**
- `calendar_client.py` — Google Calendar writes (dedicated **ROVER** calendar).
- `calcom_client.py`, `calcom_poller.py` — Cal.com booking + **60s** poll
  (PENDING→CONFIRMED, reschedule/cancel), matched on the `metadata[ref]` Cal.com echoes back.
- `scheduling.py` — orchestrator: PENDING placeholders, **date-locked** links, handoff.
- `commands.py` — Telegram booking commands (see below).

**Telegram & ops**
- `telegram_notify.py` (send cards), `telegram_poll.py` (buttons/callbacks/edits),
  `heartbeat.py` (daily heartbeat + failure alerts), `config.py`.
- **Telegram is a plain *synchronous* long-poll built on `requests`, NOT `python-telegram-bot`.**
  Runs in its own daemon thread; only callbacks from the configured chat id are honored. Keep
  it synchronous to match the rest of the service.

---

## Screening state machine

`S0 INITIAL_INQUIRY → S1 AWAITING_CONSENT → S2 AWAITING_ANSWERS → S3 POST_SCREEN`,
terminal `CONVERTED` / `NOT_SUITABLE`. SMS mirrors both sides of the conversation, so stage
inference is more reliable than the old client-only email view. Off-playbook / post-booking
messages are **flagged, not auto-answered**. Convert is also set automatically by the
confirmation marker.

**Special-case routing:**
- **Wrong-sitter detection.** `SITTER_NAME` / `SITTER_ALIASES` identify a legitimate greeting;
  a request addressed to someone else gets the fixed `{WRONG_SITTER_TEMPLATE}` archived-request
  reply instead of screening.
- **Returning clients** (a prior confirmed booking) **skip screening** and get a fixed
  template at **zero API cost** (no LLM call).

**Drafter hard rules:** availability is **always assumed** (never say you'll check dates);
**never mention pricing**; reproduce fixed templates (questionnaire, "About My Services"
policy, park meet-and-greet) near-verbatim, personalizing only bracketed fields; never invent
policy — flag anything not covered by playbook/FAQ.

---

## Approve-and-send — the safety keystone

The bot transmits on Rover (via SMS) **only** through this gate:

- **Nothing sends without an explicit Approve tap.** No auto-send path exists; do not add one.
- **Edit path:** Malik replies to the card with corrected wording → stored as `pending_text`
  → **Approve & Send** sends *that* version.
- **Idempotent:** double-tap / retry cannot double-send (guarded per `thread + pending_text`).
- **Unmistakable target:** the card names the client the message will go to.
- **Delivery-confirmed:** success acks in Telegram; failure/timeout alerts — an approved
  message must never silently vanish.

`Regenerate / Warmer / Shorter` re-draft before approval; `Converted / Not suitable` terminal.

---

## Private-booking commands (`commands.py`)

For bookings that arrive on a phone line the bot never sees (entered manually by Malik):
`/booking`, `/dropoff`, `/pickup`, `/meetgreet`, `/links`, `/retarget`, `/movebooking`,
`/cancelbooking`. They share the calendar + link machinery; private bookings use a synthetic
`thread_key` (`private:<slug>`) and never create an SMS thread or trigger drafting.

---

## Data model (SQLite — `rover_autoresponder.db`)

Confirm exact schema in `models.py` / `store.py`. Per the docs + build:

- `threads` keyed by **`thread_key` = conversation number**; plus `phone_number`,
  `email_thread_key` (nullable), `owner_name`, `pet_name`, `stay_dates`, `stage`, `status`
  (`active|converted|not_suitable`), `pending_text`, `last_draft_text`, `sent_at`,
  `send_status`, `flags`, timestamps.
- `messages` — `direction` (inbound / bot-sent outbound), `truncated` flag, marker type,
  dedupe id.
- `scheduling_events` — `thread_key`, `episode`, `source` (`rover|private`), `kind`
  (`dropoff|pickup|meet_greet`), `status` (`pending|confirmed|cancelled`), `target_date`,
  `scheduled_at`, `gcal_event_id`, `booking_ref`, `link_url`, timestamps. Unique on
  `(thread_key, episode, kind)`. Per-episode meta keys track things like `links_sent`.

SQLite is accessed from multiple threads: opened with `check_same_thread=False` **and guarded
by a lock**. Keep both if you touch the store.

---

## Calendar layer specifics (Addendum B v0.6)

- **PENDING-first:** on confirmation, immediately write transparent placeholder events
  (`<Pet> Drop-off/Pick-up (PENDING)`) to the dedicated **ROVER** Google Calendar; flip to
  opaque on confirmation.
- **Bot owns its own event end-to-end** (v0.6 reversal): Cal.com exposes no Google event id,
  so it can't relabel Cal.com's event. Cal.com writes go to a **throwaway calendar**;
  conflict-checking reads **ROVER + throwaway**.
- **Cal.com event-type locations must be "In Person"** (default is Cal Video).
- **Poll, don't webhook** for Cal.com (box is behind home NAT); 60s poller is self-healing.
- **Timezone:** Cal.com timestamps are UTC — convert to `CALENDAR_TIMEZONE` **before storing**,
  or evening bookings land on the next day.
- Triggers: SMS confirm marker + confirmation email (email = linchpin), **modifications by
  email only**, cancellations by SMS, private bookings via commands.

---

## Services & entrypoints

Three systemd **user** services in `systemd/` (linger enabled). Entrypoints, from the units:

- `rover-sms.service` → `python -m autoresponder.sms_main --serve`: the SMS pipeline —
  ingest, parse, draft, approve-send. **This service owns the Telegram poller.**
- `rover-email-fallback.service` → `python -m autoresponder.main` with **`EMAIL_MODE=fallback`**:
  ingest-only for confirmation emails, modification emails, and truncation recovery; **no
  drafting, no Telegram poll** (so it can't fight the SMS service's poller). *"fallback" is the
  mode name — the data it feeds is load-bearing (see "Email pipeline" above), not optional.*
- `rover-autoresponder.service` → `python -m autoresponder.main` with **no `EMAIL_MODE` set**.
  This is the legacy pre-cutover Phase-5 unit, **superseded** by the rover-sms + rover-email-
  fallback split. Because `EMAIL_MODE` defaults to `fallback` (`config.py`), running it today
  just duplicates rover-email-fallback's ingest — do **not** enable it alongside rover-sms. The
  old full email pipeline (drafting + its own Telegram poll) runs only under
  `EMAIL_MODE=standalone`, which no unit sets. (Its enabled/disabled symlink state is runtime,
  not in the repo — check `systemctl --user is-enabled rover-autoresponder`.)

Background workers (Cal.com poller, Telegram poll, daily `watch()` renewal, heartbeat) run as
threads inside their host service.

---

## Running & testing

- Deps: `pip install -r requirements.txt`. Tests: `python -m pytest` (see `tests/`).
- `live_scheduling_test.py` hits real Cal.com/Calendar — run deliberately, not in the suite.
- `samples/` holds real Rover messages used by parser tests; add new marker variants here as
  they're observed (regexes are still being hardened across service types).

**Secrets (`.env`):** `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, dedicated
Gmail OAuth token path, `PUBSUB_SUBSCRIPTION`, Cal.com API key, `EMAIL_MODE`, `CALENDAR_TIMEZONE`,
`SITTER_NAME` / `SITTER_ALIASES`. Plus JSON key files here (`credentials.json`, `token.json`,
`pubsub-sa.json`) and `certs/`. The real `playbook.md` / `faq.md` are gitignored; only the
`.example` copies are committed.

---

## Non-obvious invariants (learned the hard way — don't regress)

- **IPv4 preference (`netprefs.py`) must load early.** Undoing it reintroduces ~16s network
  stalls on the bridged VM.
- **SQLite** is multi-threaded: `check_same_thread=False` + a lock. Both are required.
- **Gmail 404 on deleted messages** is expected — tombstone and skip, don't crash.
- **Telegram latency fix:** answer the callback query *first*, then reuse the HTTP session
  (on top of the IPv4 fix). Don't reorder.
- **Truncation recovery matches on full message text + an owner cross-check**, not a short
  prefix (a 60-char prefix falls inside questionnaire boilerplate and cross-contaminates
  clients).
- **Recovered long messages:** card rendering is budget-aware; overflow goes as a follow-up
  message rather than being silently chopped to ~500 chars.
- **Confirmation-email parsing** must pass the full `body_text` — confirmation emails lack the
  `says:` / `Reply now` markers the ordinary-message parser keys on, which previously stored a
  NULL body.
- **Scheduling-link cards are de-duped per episode** (`links_sent` meta) — both the SMS marker
  and the confirmation email can fire for one booking.
- **`/movebooking`** only touches legs whose date actually changed (preserves already-confirmed
  times) and neutralizes the superseded Cal.com booking.

---

## Build status (verified against the code)

- Base brain + SMS transport + email fallback: **built** (drafter, sms_*, truncation, gmail/
  pubsub, tests incl. `test_phase5`, `test_email_fallback`).
- Calendar: **C4 (modification/cancellation) and C5 (private bookings) are built** — the full
  private-booking command set (`commands.py`, `test_commands.py`), the SMS `modified` marker
  (`sms_parser.py`), and Cal.com reschedule/cancel reconciliation (`calcom_poller.py`) all
  exist. **C6 (48h reminders) is not built** — addendum B lists it pending and there is no
  reminder code anywhere in `autoresponder/` (no scheduler, no `remind`/`48h` path). Don't
  assume reminders fire.
- `rover_autoresponder.db.bootalert` is **not a DB copy** — it's the boot-failure-alert
  throttle stamp written by `main._boot_alert_allowed` (one float timestamp; mutes repeat
  crash alerts for `BOOT_ALERT_INTERVAL_SEC`). It is a runtime-state file and belongs out of
  git — now gitignored (as it should be, like `rover_autoresponder.db`).