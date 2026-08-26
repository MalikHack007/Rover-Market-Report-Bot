# Design Addendum A — SMS transport & approve-and-send

**Status:** Draft v0.1 (design review — no code yet)
**Extends:** `rover_autoresponder_design.md` (v0.3)
**Owner:** Malik
**Last updated:** 2026-07-28

This addendum changes the transport and the operating mode. It supersedes v0.3 only
where noted; the "brain" (drafter, playbook, FAQ, stage machine, debounce, SQLite
state, most of the Telegram layer) is reused unchanged.

---

## 1. What changes, and why

Two shifts, driven by a capability email doesn't have:

1. **Assist → approve-and-send.** Email can't post back to a client, so v0.3 drafts and
   you copy-paste. Rover's **SMS** notifications *can* be replied to — you reply to the
   conversation's number and Rover routes it to the client. So the bot can close the
   loop: draft → you approve (or edit, then approve) → **the bot actually sends**. The
   copy-paste step disappears.

2. **Email → SMS-primary, email-fallback hybrid.** SMS becomes the primary transport
   (identity, routing, sending, and most content). Email is retained only as a
   **fallback for full message text when SMS truncates** (see §5).

**Posture note (unchanged stance, moved line):** this crosses from "you send" to "the
bot sends." Even with per-message approval, the bot now transmits on Rover for the first
time. The SMS channel is invisible to Rover's *web* anti-bot systems, and human approval
on every message keeps volume and wording human-like — but the approval gate (§6) is the
safety keystone and is designed to be idempotent and un-bypassable.

---

## 2. Findings that drove the design (from real samples)

- **Routing = unique number per conversation.** Rover assigns a number to each request;
  replying to it reaches that client. The number *is* the thread key — unambiguous send
  target, no risk of cross-delivery.
- **Number lifecycle is whole-life.** One number persists across the entire request
  (pending → confirmed → complete), unlike email, which spawns a new thread on
  confirmation. So the number alone is a stable primary key.
- **The inquiry marker.** A new request injects a machine-generated block, e.g.:
  `New booking request (boarding) from Anika: Teddy (1 yr, 60 lbs) 08/21/2026 to
  08/23/2026. Book @ r.rover.com/8C48qS`, plus a `Boarding Request - One Time:` block.
  This is the SMS equivalent of the old email subject tell — it **opens** an inquiry, and
  hands us structured fields (owner, pet, age/weight, dates, booking URL) inline.
- **The confirmation markers.** Machine lines like `… has confirmed a booking request
  (stay) with Alfie …` and `Your upcoming booking … has been modified …` mark a request
  as booked/changed. These **close** the inquiry.
- **Ordinary client messages carry no markers.** Confirmed conversations' ongoing texts
  have no markers in the body — so "no marker + already converted" stays quiet safely.
- **SMS truncates long messages.** A long client message is cut off with a
  `… He… (more at https://r.rover.com/NWPXeH )` tail. The `more at` link is
  **cookie-gated** and not openable from an automated workflow. Email is *not* truncated.
- **Multi-message openers are normal.** Anika's inquiry arrived as four texts including a
  delayed afterthought ("I forgot to add my kitten"). Debounce is essential and its
  window must be generous enough to catch a straggler.

---

## 3. Architecture (updated)

```
   ┌─────────────────────────────────────────────────────────────┐
   │  Rover (per-conversation numbers; SMS mirrors the thread)    │
   └───────────────┬───────────────────────────────▲─────────────┘
      inbound SMS  │                                │ approved reply (SMS)
                   ▼                                │
        ┌──────────────────────┐          ┌────────────────────┐
        │  Android phone        │  send    │  Android phone      │
        │  (gateway: forward    │◀─────────│  (gateway: send API)│
        │   inbound -> Linux)   │          └─────────▲──────────┘
        └───────────┬──────────┘                     │ send(number,text)
     HTTP (LAN/tunnel, no cloud relay)               │
                    ▼                                 │
   ┌───────────────────────────────────  LINUX BOX  ─┴───────────────────────┐
   │  SMS ingest  ->  parser + marker state machine  ->  SQLite (by number)   │
   │                         │                                                │
   │                         ▼ (active inquiry, not converted)                │
   │   debounce  ->  DRAFTER (playbook + FAQ + stage)  ->  Telegram card       │
   │                                                          │  approve/edit  │
   │                                          ┌───────────────┘                │
   │                                          ▼                                 │
   │             SEND ADAPTER  send(number, approved_text) + delivery callback  │
   │                                                                            │
   │   EMAIL FALLBACK (existing Gmail pipeline): full text on SMS truncation,   │
   │                    correlated to the SMS number once per request           │
   └────────────────────────────────────────────────────────────────────────┘
```

Brain stays on the Linux box. The phone is a dumb SMS modem: forwards inbound texts
(with sender number) to the box, and exposes a send-to-number endpoint.

---

## 4. Components

### 4.1 SMS gateway (phone-as-gateway)
The Android runs a self-hosted SMS-gateway app that (a) POSTs each inbound SMS
`{from_number, body, timestamp}` to the Linux box, and (b) accepts a request to send a
text to a number. Connectivity is **LAN or a private tunnel — no third-party cloud
relay**, to keep client messages flowing only through your phone and your box (matching
the isolation of the email side). App selection is deferred (its own step); the box side
is abstracted behind a `send(number, text) -> delivery_callback` interface so the actual
mechanism (gateway app / Tasker / ADB) can be swapped without touching the brain.

### 4.2 Identity
Thread key = the client's assigned **number**. One number, whole lifecycle, primary key.
(Assumption: numbers are not recycled across different clients over time. If they are, we
key on `number + first-seen window` — deferred until observed.)

### 4.3 Classifier — in-thread marker state machine
SMS has no subject, so classification runs over **in-thread markers** instead:

```
 (new number seen)
        │  body contains "New booking request (…) from … Book @ r.rover.com/…"
        ▼
   ACTIVE INQUIRY  ── ordinary client messages ──▶  draft (multi-turn)
        │
        │  body contains "has confirmed a booking request …"  OR
        │  "Your upcoming booking … has been modified …"
        ▼
   CONVERTED  ──▶  stop drafting; ignore further messages
        │  (or you tap Not suitable at any point ──▶ NOT_SUITABLE, stop)
```

Owner/pet/dates are parsed from the inline booking-request block (cleaner than email,
which required LLM inference). Exact marker regexes finalize once we have a few more
samples of each marker variant (service types beyond boarding, etc.).

### 4.4 Drafter / debounce / state — reused
Unchanged from v0.3: the drafter, playbook, FAQ, stage machine, and debounce all apply.
Debounce keeps its role and gains importance (multi-message openers with stragglers).
Because SMS **mirrors the full conversation including your sent replies**, the drafter now
sees both sides — so stage inference is more reliable than the client-only email view, and
the old "Mark sent" hint is largely unnecessary (§6).

### 4.5 Send adapter
`send(number, text)` posts to the gateway's send endpoint and registers a **delivery
callback**. A send is only considered done on a delivery/queued confirmation; a
failure/timeout raises a Telegram alert (an approved message must never silently vanish).
Sends are idempotent per (thread, draft) so a retry or double-tap can't double-send.

---

## 5. Truncation handling (email fallback)

SMS truncates long messages and the `more at …` link is cookie-gated, so the full text is
recovered from the **existing Gmail pipeline** (email is not truncated).

- **Detect:** an inbound SMS ending in the `… (more at https://r.rover.com/…)` pattern is
  flagged truncated.
- **Recover:** pull the corresponding full message from the correlated email thread.
- **Correlate once per request:** SMS (keyed by number) and email (keyed by Gmail thread)
  share content — client name, pet, dates from the booking-request block. Match on those
  **once** to bind number ↔ email thread; because the SMS number is stable for the
  request's whole life, the mapping then holds for every later message. So correlation is
  one-time per request, not per message.

If a request never produces a truncated SMS, the email side is never needed for it. Email
is a fallback, not a parallel source of drafting triggers.

---

## 6. Approval & send UX (Telegram)

The card grows from "display + buttons" to "approve or edit, then send":

- Draft arrives as today, but the primary action is **✅ Approve & Send** (replaces
  "Mark sent"). Tapping it sends the current text to the conversation's number.
- **Edit path:** you reply to the card in Telegram with corrected wording; the bot stores
  it as the pending text and re-shows it; **Approve & Send** then sends *your* version.
- **Regenerate / Warmer / Shorter** carry over (re-draft before approving).
- **🎉 Converted / 🚫 Not suitable** carry over (terminal; stop drafting). Converted is
  also set automatically by the confirmation marker (§4.3).

Safety keystone — the approval gate:
- **Nothing sends without an explicit Approve tap.** No auto-send path exists.
- **Idempotent:** double-tap / retry cannot double-send (guarded per thread+draft).
- **Unmistakable target:** the card names the client the message will go to.
- **Delivery-confirmed:** success acks in Telegram; failure alerts.

Because the bot records its own sends directly, it no longer guesses whether you sent —
"Mark sent" collapses into "Approve & Send."

---

## 7. Data model deltas (SQLite)

- `threads.thread_key` → the **conversation number** (was Gmail thread id).
- Add `phone_number` (redundant with key, but explicit) and `email_thread_key`
  (nullable; set on first correlation for the fallback).
- Add `pending_text` (the current draft or your edited version awaiting approval) and
  `sent_at` / `send_status` (for idempotency + delivery tracking).
- `messages` gains `direction` use for real (inbound vs. bot-sent outbound), `truncated`
  flag, and stores the marker type when present.
- `status` values unchanged in spirit: `active` | `converted` | `not_suitable`.

---

## 8. Carry-over vs. change (summary)

| Area | v0.3 (email) | Addendum A (SMS) |
|---|---|---|
| Read transport | Gmail push + Pub/Sub | Android SMS gateway (email = fallback only) |
| Identity | Gmail thread id | conversation number (whole lifecycle) |
| Classifier | email subject pattern | in-thread marker state machine |
| Owner/pet/dates | LLM-inferred | parsed from booking-request block |
| Drafter/playbook/FAQ/debounce/state | — | reused unchanged |
| Content completeness | full (email) | full except long msgs → email fallback |
| Operating mode | draft → you paste | draft → approve/edit → **bot sends** |
| "Mark sent" | advances stage hint | becomes **Approve & Send** |

---

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Bot now transmits on Rover (ToS/account) | Per-message human approval; no auto-send; SMS invisible to web anti-bot; human-like cadence. Your accepted trade-off. |
| Approved message silently fails to send | Delivery callback required; failure/timeout → Telegram alert; send idempotent. |
| Double-send on retry/double-tap | Idempotency guard per (thread, draft/pending_text). |
| Long client message truncated in SMS | Detect `more at …`; recover full text via correlated email. |
| Number recycled across clients over time | Assume unique now; if observed, key on number + first-seen window. |
| Bridge (phone↔box) is now the read/send path | Health-check the gateway link; heartbeat covers it; alert on outage. |
| Android SMS-send permissions vary by version | Verify chosen app can send on your Android version (app-selection step). |
| Email↔SMS correlation mismatch | Correlate on client/pet/dates once per request; stable number makes it one-time. |
| Marker variants we haven't seen (other services) | Finalize regexes from more samples; unmatched opener → treat conservatively, flag. |

---

## 10. Open items (before/'during build)

1. **Gateway app selection** — self-hosted HTTP SMS gateway vs. Tasker vs. ADB;
   send-permission behavior and delivery-receipt support on your Android version.
2. **Finalize marker regexes** — collect a few more inquiry/confirmed/modified samples
   across service types (boarding, day care, etc.).
3. **Confirm number non-reuse** over a longer horizon (else add the first-seen window).
4. **Correlation fields** — confirm client/pet/dates appear identically enough in both SMS
   block and email to match reliably.

---

## 11. Phasing (SMS migration; email pipeline keeps running in parallel)

- **S1 — Gateway bridge.** Phone forwards inbound SMS → box; box can send via the phone.
  Ingest + log only. Prove the transport both directions.
- **S2 — Parser + identity + marker state machine.** Classify by number; open on
  booking-request marker, convert on confirmation/modified marker. No drafting yet.
- **S3 — Wire the brain.** Reuse drafter/playbook/FAQ/debounce; draft to Telegram, no send.
- **S4 — Approve-and-send.** Approve/edit UX, send adapter, delivery confirmation,
  idempotency guard. This is the first turn the bot transmits.
- **S5 — Truncation + email fallback.** Detect truncation; correlate number ↔ email thread;
  recover full text.
- **S6 — Cutover.** SMS becomes primary; Gmail relegated to fallback-only. Decide whether
  to retire the Pub/Sub trigger or keep it solely for truncation recovery.