# Design Addendum B — Calendar & scheduling integration

**Status:** Draft v0.5 (design review — no code yet)
**Extends:** `rover_autoresponder_design.md` (v0.3) + Addendum A (SMS transport)
**Owner:** Malik
**Last updated:** 2026-08-20

**Changelog v0.4 → v0.5:** Simplified private bookings (§8). They arrive on a **different
phone line the bot never sees**, so there is no SMS thread, no drafting, and no
approve-and-send involved — which removes the accidental-re-screening risk entirely and
drops the `source` exclusion machinery. The bot's only job is: record the booking, place
the calendar events, and hand you links to send yourself.

**Changelog v0.3 → v0.4:** Booking updates now come from **polling Cal.com**, not webhooks —
Cal.com's cloud can't reach a LAN box behind home NAT, and polling avoids exposing a public
endpoint (§4.1). Added **private (non-Rover) bookings** (§8): manually created from Telegram,
same calendar + link flow, but explicitly excluded from the screening playbook.

**Changelog v0.2 → v0.3:** Corrected a wrong assumption — **modification emails arrive as
a SEPARATE email thread**, not inside the conversation thread, so the phone↔thread binding
does not correlate them. Replaced with a **dual strategy** (§7.1): owner+pet matching as
the immediate trigger, plus **date-drift detection** on ordinary message notifications as
a continuous safety net. Availability rules finalized (§6.1).

**Changelog v0.1 → v0.2:** All §11 questions answered — decisions locked in §12. Added the
confirmation-email and cancellation-SMS formats. Two findings changed the design: the
confirmation email carries the **client's phone number** (reliable correlation key) and
**full dates with the year** (removes the year-inference guesswork). Added the PENDING →
tool-owned-event **handoff** (§5.1) implied by decision 4.

---

## 1. Goal

Stop negotiating drop-off / pick-up / meet-and-greet times by text. Instead:

1. A booking is confirmed → the bot drafts a message containing a **scheduling link**.
2. **Immediately** — before the client does anything — the bot writes placeholder events
   to Google Calendar: `Archie Drop-off (PENDING)` on the start date, `Archie Pick-up
   (PENDING)` on the end date.
3. When the client picks a 30-minute window, those events move to the chosen time and
   become `Archie Drop-off (CONFIRMED)`.
4. Same pattern for meet-and-greets, triggered during screening rather than on booking.
5. You can block out recurring windows so those times are never offered.

The PENDING-first design is deliberate: your calendar shows every commitment the moment
it exists, even if the client never clicks. A booking you'd otherwise forget about can't
hide.

---

## 2. Triggers

| Trigger | Source | Creates |
|---|---|---|
| Booking confirmed | SMS marker: `[ Jessica K. has confirmed a booking request (stay) with Archie from 09/01 to 09/06 … ]` | Drop-off + Pick-up PENDING events, drop-off/pick-up links |
| Meet-and-greet requested | Client asks during screening (S3) | M&G PENDING event (tentative date) + M&G link |
| Client books a slot | **Cal.com poll** (§4.1) | PENDING → CONFIRMED, event moved to chosen time |
| Client reschedules/cancels a slot | **Cal.com poll** | Event moved, or reverted to PENDING |
| **Private booking entered** | You, via Telegram command (§8) — private clients use a different phone line the bot never sees | Drop-off + Pick-up PENDING events + links for you to send |
| **Booking modified** | **Email only**: `Your revised itinerary … Dates: Aug 5, 2026 - Aug 10, 2026` | Move events to new dates; re-issue links |
| Booking cancelled | SMS: `[ Rover Update: Your booking from 08/15/2026 to 08/18/2026 with Joshua K. has been cancelled … ]` | Delete events, expire links |
| Booking confirmed (email) | Subject `Confirmed: Mazzy's upcoming booking from Aug 20, 2026 - Aug 23, 2026`; body has **Owner, Phone number, Dates** | Binds phone ↔ email thread; supplies authoritative dates |

Note the asymmetry that shapes §7: **confirmations arrive by both SMS and email, but
modifications only by email.** The email fallback pipeline is therefore load-bearing for
calendar correctness, not just truncation recovery.

**The confirmation email is the linchpin.** It contains the client's *phone number* —
which is our SMS thread key — plus the owner name, pet name, and full dates *with the
year*. So at confirmation we bind `phone ↔ conversation email thread` deterministically,
and we get authoritative dates (the SMS says `09/01 to 09/06`; the email says
`Aug 20, 2026 - Aug 23, 2026`), removing the year-inference problem.

**Scope of that binding — important:** it binds the *conversation* thread only.
**Modification ("revised itinerary") emails arrive as a separate, standalone email**, not
inside that thread, so they are NOT correlated by this binding. See §7.1 for how they are
handled instead.

**Ordering:** whichever arrives first (SMS or email) creates the events; the other
enriches. If the SMS lands first we create events on inferred dates and correct them when
the email confirms; if the email lands first we already have authoritative dates.

---

## 3. Architecture

```
   SMS confirm marker ─┐
   M&G request (S3) ───┤
                       ▼
              ┌──────────────────┐   creates    ┌────────────────────────┐
              │ Scheduling       │─────────────▶│ Google Calendar        │
              │ Orchestrator     │  PENDING     │  "Archie Drop-off      │
              │                  │  events      │   (PENDING)"           │
              └────────┬─────────┘              └──────────▲─────────────┘
                       │ builds link                       │ move + retitle
                       ▼                                   │ (CONFIRMED)
              ┌──────────────────┐                ┌────────┴─────────┐
              │ Draft + approve  │                │ Cal.com POLLER   │
              │ (existing S4)    │                │ (every ~60s)     │
              └────────┬─────────┘                └────────▲─────────┘
                       │ approved SMS                      │ pulls bookings
                       ▼                                   │
                    CLIENT ──── opens link ───▶ Cal.com (hosted)

   Private bookings (§8): you → /booking command → Orchestrator → calendar + links
     (separate phone line; bot never messages these clients — you send the links)
                                                           │
   Availability sources ──────────────────────────────────┘
     • recurring rules (your blocked windows)
     • Google Calendar busy times
     • already-taken slots
```

The orchestrator is new. Everything else reuses the existing pipeline: links ride out
inside a normal approve-and-send draft, and the Cal.com poller runs as a background thread
alongside the existing debouncer and Telegram poller.

---

## 4. The scheduling layer — the key fork

The client needs a **publicly reachable** page. Your VM is behind home NAT, so this is a
real constraint, not a detail.

### Option A — Cal.com (hosted), one event type per purpose *(recommended)*
Three event types: `dropoff`, `pickup`, `meet-greet`. Links carry the date as a
parameter (`?date=2026-09-01`), and the poller validates that the client booked on the
right day.

- **Pros:** no public hosting to run, availability rules + conflict detection built in,
  free tier covers it, reschedule/cancel handled for you.
- **Cons:** the date parameter *pre-selects* rather than *enforces* — a determined client
  could navigate to another day. Mitigation: the poller flags a date mismatch and alerts
  you rather than silently accepting it.

### Option B — Cal.com, one event type created per booking via API
Each booking gets its own event type locked to a single date, deleted afterwards.

- **Pros:** the date really is enforced.
- **Cons:** API churn (create + delete per booking), slug management, more failure modes,
  and clutter if cleanup fails.

### Option C — Self-hosted mini scheduling page
Serve our own page from the VM through a tunnel (e.g. Cloudflare Tunnel) for a public URL.

- **Pros:** exact semantics, no third party, all data stays with you.
- **Cons:** we build slot generation, conflict checking, reschedule/cancel, and a public
  attack surface — plus tunnel uptime becomes another dependency.

**Recommendation: A.** Start with the lowest build cost and a validation safety net; move
to B or C only if clients actually book wrong days in practice.

---

### 4.1 Updates by polling, not webhooks

Cal.com fires webhooks from its cloud to a public URL. Our receiver sits on a **LAN IP
behind home NAT with a self-signed cert**, so Cal.com cannot reach it. Rather than expose
a public endpoint (Cloudflare Tunnel or similar), the orchestrator **polls Cal.com's
bookings API** on a short interval.

- **Interval:** ~60s. Latency from "client books" to "event flips to CONFIRMED" is at most
  a minute, which is irrelevant here — nobody is watching the calendar in real time.
- **No public attack surface**, no tunnel to keep alive; consistent with the
  brain-stays-on-the-LAN posture of the rest of the system.
- **It doubles as the reconcile job** (§9): because each poll compares Cal.com's state
  against ours, drift is corrected as a side effect rather than needing a separate task.
- **Idempotent:** each poll processes bookings by Cal.com booking id; an already-handled
  booking is a no-op, so a repeated poll cannot double-apply anything.
- Detects all three transitions in one place: new bookings, reschedules, and cancellations.

**Matching a Cal.com booking back to our event:** each generated link carries a **reference
token** (a Cal.com metadata/prefill parameter holding our `scheduling_events.id`). The
poller reads it straight off the booking, so matching is exact rather than inferred from
name/date coincidence. Works identically for Rover and private bookings. If a booking
somehow arrives without the token (client booked from a bare link), fall back to matching
on date + event type and alert if ambiguous.

If real-time ever matters, a tunnel + webhook can be added later without changing anything
downstream — the handler is the same.

---

## 5. Event lifecycle

```
  booking confirmed / M&G requested
            │
            ▼
      [PENDING]  ── placeholder on the calendar, link sent to client
            │
            ├── client books a slot ─────────▶ [CONFIRMED]  (event moved + retitled)
            │                                       │
            │                                       ├── client reschedules ─▶ [CONFIRMED] (moved)
            │                                       └── client cancels ─────▶ [PENDING] (back to placeholder)
            │
            ├── booking dates modified (email) ─▶ [PENDING] on the NEW dates, link re-issued
            │        (if a time was already confirmed, see §9 "modification after confirm")
            │
            └── booking cancelled ─────────────▶ [DELETED]
```

Titles encode state so your calendar is readable at a glance:
`{Pet} {Type} (PENDING|CONFIRMED)` — e.g. `Archie Drop-off (CONFIRMED)`.

### 5.1 The PENDING → CONFIRMED handoff (from decision 4)

You chose "the tool owns the event, the bot relabels." That's clean *after* a booking, but
before the client picks a time **the tool has no event** — so the bot must own the
placeholder, and there's a moment where two events exist:

```
  1. Booking confirmed        → BOT creates "Archie Drop-off (PENDING)"
                                 (30-min block, transparent, Rover calendar)
  2. Client picks 3:00 PM     → CAL.COM creates its own event at 3:00 PM
  3. Webhook fires            → BOT deletes its placeholder
                              → BOT retitles Cal.com's event to
                                "Archie Drop-off (CONFIRMED)"
```

Requirements this imposes:
- Cal.com's Google Calendar write target must be the **Rover calendar**, or the confirmed
  event lands somewhere else and your calendar splits across two places.
- The delete-then-retitle must be **atomic in effect**: if the retitle fails after the
  delete, you'd be left with an unlabelled Cal.com event. Retry, and alert on failure.
- If a poll is missed, you'd briefly have a PENDING placeholder *and* a real Cal.com event;
  the next poll reconciles it automatically (§4.1).

---

## 6. Availability model

Three inputs decide which 30-minute slots the client sees:

1. **Recurring blocks you define** — your stated rules (see §6.1). In Option A these map
   to Cal.com availability schedules, one per event type.
2. **Google Calendar busy times** — your real commitments.
3. **Slots already taken** by other clients.

> **Critical feedback loop:** our own PENDING/CONFIRMED events live on Google Calendar. If
> the scheduling tool treats them as busy, the bot would block the very slots it's trying
> to offer — and a PENDING placeholder would block its own confirmation. **Mitigation:**
> put bot-created events on a **dedicated "Rover" calendar** that the scheduling tool does
> *not* consult for conflicts, or mark them free/transparent. This must be settled before
> implementation; it's the most likely source of a confusing bug.

A second loop to avoid: the tool creates its own event when a client books (§5.1). Its
write target is the **Rover calendar**, and the bot deletes its own placeholder on the
poll so only one event survives.

### 6.1 Your availability rules

Drop-off, pick-up, and meet-and-greet are available **every day**, on the general windows —
except **Tuesdays and Wednesdays**, which use a narrower set:

```
Mon, Thu, Fri, Sat, Sun          Tue, Wed  (narrow)
   9:00a – 12:00p                   9:00a – 11:00a
   2:00p –  5:00p                   5:00p –  7:00p
   7:00p – 11:00p
```

The narrow rule applies to **all three** event types on those days. In Cal.com this is one
availability schedule with per-weekday hours, shared by the three event types (or
duplicated per type if you later want them to diverge).

Slots are **30 minutes**, with a **30-minute buffer** between them (configurable; back-to-
back is acceptable). Note the buffer meaningfully reduces capacity in the narrow windows —
a 9:00–11:00a window yields 2 slots with buffers vs. 4 back-to-back. Worth watching in
practice; it's a one-line config change if it turns out to be too tight.

---

## 7. Dates — the fiddliest part

- **Confirmation SMS has no year:** `from 09/01 to 09/06`. Used only if the confirmation
  email hasn't arrived yet; we infer the next future occurrence (rolling the end date into
  the following year for ranges crossing New Year), then **correct from the email**, which
  is authoritative (`Aug 20, 2026 - Aug 23, 2026`).
- **Cancellation SMS carries full dates with the year** (`from 08/15/2026 to 08/18/2026`)
  plus the owner name — enough to identify the booking directly.
- **Modifications arrive only by email**, as a **separate standalone email** (not in the
  conversation thread), with full dates (`Aug 5, 2026 - Aug 10, 2026`), the pet name
  (`Luckie`) and owner (`Jennifer`) — but **no phone number**. Correlation strategy in §7.1.

### 7.1 Correlating a modification to a booking — dual strategy

No single signal is both immediate and reliable, so we use two that fail in different ways.

**Primary — owner + pet match (immediate).**
The revised-itinerary email gives pet name, owner name, and the new dates. Match against
threads that currently hold an **active/upcoming booking**, requiring **both** names.

- Names are normalized (first name only, case/punctuation stripped) because Rover renders
  them inconsistently across channels (`Jennifer` vs `Jennifer K.`).
- Restricting to bookings that are *currently upcoming* eliminates most collisions — the
  realistic risk isn't "two clients named Jennifer ever", it's "two upcoming bookings with
  the same owner AND pet name at once", which is vanishingly unlikely.
- **Ambiguous or zero matches → do not guess.** Alert with the email contents so you can
  resolve it. (Same philosophy as truncation correlation: a wrong match moves the wrong
  client's calendar events, which is worse than asking.)
- Known gap: the ~5% of threads with no pet name (marker never arrived) can't match on pet.
  Those fall through to the safety net below, and the name-recovery layers (Addendum A)
  reduce how often this happens.

**Safety net — date drift on message notifications (continuous).**
Every message notification in the *conversation* thread leads with the booking's current
dates:

```
Hi Yujie,
Jennifer sent you a message about a stay from 08/05/2026 to 08/09/2026.
```

That thread **is** bound to the phone number (§2), so no fuzzy matching is involved. If
those dates differ from what we have stored for the booking, the booking was modified —
regardless of whether we ever saw or correlated the revised-itinerary email.

- Catches modifications we failed to correlate, or whose email we missed entirely.
- Costs nothing: we already ingest these emails for truncation recovery.
- Weakness: only fires when the client next messages. If they modify and go silent, this
  never triggers — which is exactly why it's the *net*, not the primary.

**Combined behavior:** the primary fires immediately when it can; the net corrects drift
whenever a message arrives. Either path lands in the same handler (move events to the new
dates, re-issue links, alert if a time was already confirmed).
- **Time zone:** all events written in your local zone (America/Chicago). Clients booking
  from elsewhere see their own zone; the tool handles conversion.
- **Same-day bookings** (day care): drop-off and pick-up land on the same date — two
  events, not one, and the pick-up link must not offer times before the drop-off.

---

## 8. Private (non-Rover) bookings

You also board clients booked directly. **They contact you on a different phone line that
the bot never sees**, so nothing about them arrives automatically and no SMS thread exists.

That absence is a feature: because there's no thread, there is **no possibility of the
screening playbook firing at a long-standing private client**. No exclusion logic needed —
the pipeline simply never learns about them.

### 8.1 What the bot does and doesn't do

**Does:**
- Record the booking (pet, owner, dates) when you enter it.
- Create the two **PENDING** calendar events on the Rover calendar, exactly like a Rover
  booking.
- Generate drop-off / pick-up **scheduling links** and show them to you to copy.
- Flip them to **CONFIRMED** when the client books, via the same Cal.com poller (§4.1) —
  it doesn't care who sent the link.

**Doesn't:**
- Message the client. You send the links yourself from your other phone.
- Draft, screen, or reply to anything.

### 8.2 Why it's worth doing at all

The payoff is a **single source of truth for availability**. A private drop-off consumes a
real slot; if the bot didn't know about it, Cal.com would happily offer that window to a
Rover client and you'd be in two places at once. Entering private bookings keeps one
calendar and one set of windows honest across both sides of the business.

### 8.3 Entry

A Telegram command — no phone number, since the bot won't be texting anyone:

```
/booking Willow 2026-09-01 2026-09-06 Sarah
          pet   start      end        owner (optional)
```

The bot replies with a card showing the parsed booking and the two links, ready to copy
into your other phone. Companion commands: `/bookings` (list upcoming),
`/movebooking <id> <start> <end>`, `/cancelbooking <id>` — these stand in for the Rover
signals (itinerary email, cancellation SMS) that don't exist here.

### 8.4 Identity

Private bookings have no phone-number thread key, so they get a **synthetic key**
(`private:<slug>`, e.g. `private:willow-20260901`). It never collides with an SMS thread
key (those are always `+1…`), and it keeps the same `scheduling_events` shape so the
calendar and poller code paths are shared rather than duplicated.

Reminders (48h) apply the same way, surfacing as a Telegram nudge for you to act on —
though here you send the follow-up yourself.

---

## 9. Data model (additions)

```
scheduling_events
  id             INTEGER PK
  thread_key     TEXT        -- the client's number (links to the SMS thread)
  episode        INTEGER     -- which booking (numbers are reused per client)
  source         TEXT        -- rover | private  (private = manually entered, §8)
  kind           TEXT        -- dropoff | pickup | meet_greet
  status         TEXT        -- pending | confirmed | cancelled
  target_date    TEXT        -- the date the slot must fall on (null for M&G range)
  scheduled_at   TEXT        -- chosen start time, null while pending
  gcal_event_id  TEXT        -- for move/retitle/delete
  booking_ref    TEXT        -- scheduling-tool booking id (for reschedule/cancel)
  link_url       TEXT
  created_at / updated_at
```

`(thread_key, episode, kind)` is unique — one drop-off per booking.

Private bookings use a **synthetic `thread_key`** (`private:<slug>`) and never create an
SMS thread, so no drafter-exclusion logic is required — the pipeline never sees them (§8).

---

## 10. Edge cases

| Case | Handling |
|---|---|
| Client never uses the link | Event stays PENDING. Reminder nudge before the date (Q5). |
| **Modification after a time was confirmed** | Dates moved → the confirmed slot may be invalid. Revert to PENDING on the new date, re-issue the link, and **alert you** — this is a human-judgement moment. |
| Modification email can't be correlated to a thread | Alert with the raw email; never guess which booking to move. The date-drift net (§7.1) may still catch it later. |
| Modification email matches two upcoming bookings | Ambiguous → alert, don't guess. |
| Client modifies then never messages again | Primary path (owner+pet) is the only chance; if it failed, the alert is your signal to fix it manually. |
| Date drift detected but no modification email seen | Treat the drift as authoritative — move the events and alert. |
| Client books the wrong day (Option A) | Webhook validates against `target_date`; mismatch → keep event, flag on Telegram. |
| Two clients race for one slot | Scheduling tool is the source of truth; loser sees the slot gone. |
| Duplicate confirmation marker | Idempotent on `(thread_key, episode, kind)` — no duplicate events. |
| Booking confirmed for a returning client | Episode scoping already separates bookings; new episode → new events. |
| Multiple dogs, one booking | One drop-off event covering both (title uses both names). |
| Same-day day care | Pick-up slots constrained to after the drop-off time. |
| Booking confirmed before this feature existed | No backfill; applies to new confirmations only. |
| Google Calendar API fails | Retry with backoff; on persistent failure alert — never silently skip an event. |
| Client cancels their slot | Event reverts to PENDING (the booking still exists). |
| Private client texts you | Arrives on your other line — the bot never sees it. No risk of auto-screening. |
| Private booking modified/cancelled | No Rover signal exists — you use `/movebooking` / `/cancelbooking`. |
| You forget to enter a private booking | Cal.com may offer that slot to a Rover client — the one real failure mode of manual entry (§8.2). Mitigation: enter it when you confirm it. |
| Private and Rover client want the same slot | Whoever books first wins; the other sees it gone. Shared availability is the point. |
| Link used after the date passed | Links expire at the target date; expired link → alert you. |
| M&G that never converts to a booking | M&G event is independent; if the thread goes `not_suitable`, delete it. |
| Booking cancelled | Cancellation SMS carries owner + full dates → delete both events, expire links, alert. |
| A poll is missed (service restart, network blip) | The next poll re-reads current state, so nothing is lost — polling is inherently self-healing, unlike a dropped webhook. |
| Cal.com API unreachable for a while | Retry with backoff; alert if it persists, since scheduling silently stops working otherwise. |
| Retitle fails after placeholder delete | Retry; on persistent failure alert — an unlabelled event on the calendar is confusing but recoverable. |
| Confirmation email arrives before the SMS marker | Email path creates the events; the SMS marker is then a no-op (idempotent on `(thread, episode, kind)`). |
| Client books a slot, then the booking is cancelled | Delete the tool booking too (or alert), so the slot is freed for others. |

---

## 11. What reuses existing machinery

- Links go out through the **existing approve-and-send flow** — you still review every
  message. The link is generated *before* drafting so the drafter can include it.
- The Cal.com poller mirrors existing background workers (the debouncer, the Telegram
  poller, the daily watch-renewal) — same threading and alerting patterns.
- Modification-email correlation reuses the **content/name matching** from truncation
  recovery.
- Alerts reuse the **Telegram alert path**.

---

## 12. Decisions log

1. **Scheduling layer:** **Option A** — Cal.com hosted, one event type per purpose,
   date-parameterised links; the poller validates the booked date against `target_date`.
2. **PENDING placement:** a **default 30-minute time block**, marked **transparent** (free)
   so it never blocks its own confirmation or other slots.
3. **Calendar:** a dedicated **"Rover" calendar** — keeps the §6 feedback loop clean.
4. **Event ownership:** the **tool owns the confirmed event**; the bot creates the PENDING
   placeholder and relabels on booking (handoff in §5.1). Cal.com's write target must be
   the Rover calendar.
5. **Reminders:** yes — if no slot is booked **48h before** the date, draft a nudge for
   your approval (it goes out through the normal approve-and-send flow).
6. **Cancellations:** SMS marker, format captured in §2.
7. **Availability:** finalized in §6.1 — every day on the general windows; Tue/Wed narrowed
   to 9–11a and 5–7p for all three event types.
8. **Meet-and-greet:** open range (**next 7 days**), not date-locked.
9. **Buffers:** **30 minutes** between slots (configurable; back-to-back acceptable).
10. **Booking updates:** **poll Cal.com (~60s)** rather than webhooks — no public endpoint,
    self-healing, doubles as the reconcile job (§4.1).
11. **Private bookings:** **manually entered** via Telegram command (they arrive on a phone
    line the bot never sees). Bot records them, places calendar events, and hands you links
    to send yourself — no messaging, no drafting, no exclusion logic needed (§8).

---

## 13. Open items before build

1. **Cal.com account setup** — three event types (`dropoff`, `pickup`, `meet-greet`), one
   availability schedule matching §6.1, Google Calendar connected with **writes pointed at
   the Rover calendar**, and an **API key** (used by the poller; no webhook needed).
2. **Google Calendar API scope** — add `calendar.events` to the existing OAuth app, create
   the dedicated "Rover" calendar. (Adding a scope invalidates the current token, so a
   one-time re-consent is needed.)
3. **Phasing** — suggested order: **C1** calendar client + PENDING events; **C2** link
   generation wired into drafts; **C3** Cal.com poller (PENDING → CONFIRMED handoff);
   **C4** modification + cancellation handling (§7.1 dual strategy); **C5** private-booking
   commands (§8); **C6** 48h reminders.

4. **Private-booking command syntax** — confirm the one-line format in §8.3 works for you,
   or say if you'd prefer a guided prompt (bot asks pet → dates → owner one at a time).