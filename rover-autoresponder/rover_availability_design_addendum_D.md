# Design Addendum D — Client-facing availability calendar

**Status:** v0.3 (D0 proven live; **D1 built** — publisher + `calcom_client.available_days`
+ tests; next: D2 R2 hosting)
**Extends:** `rover_autoresponder_design.md` (v0.3) + Addendum A (SMS) + Addendum B (calendar, v0.7)
**Owner:** Malik
**Last updated:** 2026-09-20

A public, **read-only month calendar** a prospective client can open to see which days Malik
is available for boarding — unavailable days greyed out. No login, no messaging, no booking.
The client eyeballs their intended span against the greyed days.

---

## 1. Goal & the one key decision

**Goal:** let a client answer "is he free around these dates?" themselves, without a
back-and-forth, from a link Malik can drop in a Rover/SMS reply.

**The source-of-truth decision (settled 2026-09-20).** Malik keeps **two** calendars — a
personal calendar and the ROVER calendar — and **Cal.com already does cross-calendar conflict
checking across both** to decide which drop-off/pick-up slots to offer. Rather than re-read
those calendars directly and re-implement "is this day blocked" (and get the personal-calendar
half wrong), **this feature asks Cal.com the same question it already answers**:

> **A day is AVAILABLE ⇔ Cal.com can offer at least one free slot on that day.**
> **A day is UNAVAILABLE ⇔ Cal.com offers zero slots that day** — which happens exactly when
> an **all-day event on either calendar** (or a full day of bookings) blocks the whole day.

This is the reuse the owner asked for: the availability engine is Cal.com's, already trusted
for scheduling, and it already honors **both** calendars. We add no new notion of
"availability" and read no calendar directly.

### Why "zero slots = unavailable" is the right rule (and its one subtlety)

A **partial-day** busy event (a 2 pm dentist appointment) blocks the 2 pm slot but leaves the
rest of the day's slots free → the day still returns slots → **still shown available**. Only a
block that zeroes out the *whole* working window — i.e. an **all-day event** on ROVER or the
personal calendar, or a day booked wall-to-wall — drops the count to zero. That is precisely
the "blocking all-day event" semantics the owner described, now extended for free across both
calendars. So the probe must offer a **wide daily window** (see §4) — otherwise a single
mid-day appointment could accidentally zero a short window and grey out an otherwise-open day.

---

## 2. Non-goals & hard-rule compliance

- **Read-only. No booking, no messaging, no PII.** The page shows dates and a boolean per day
  and nothing else. This is a **public surface**, so the privacy keystone is: the published
  data contains **no event titles, no client names, no booking details** — only
  `{date: available}`. (§6, §8.)
- **No range picker / no "request these dates" button** (per owner, 2026-09-20). A month grid
  with greyed days only. Keeps the surface trivially safe and cheap.
- **No Rover web automation** (root CLAUDE.md) — untouched; data comes from Cal.com + Malik's
  own calendars.
- **No unattended sending** — untouched; this feature never sends anything to anyone.
- **Does not expose the LAN box.** The box is behind home NAT (the reason Cal.com is *polled*,
  Addendum B §4.1). We keep that: the box **pushes** a static file out to R2; nothing inbound.

---

## 3. Architecture

```
                    (box, behind home NAT — outbound only)
  ┌──────────────────────────────────────────────────────────┐
  │  availability publisher  (new: autoresponder/availability/)│
  │   1. calcom_client.available_days(probe_event_type,        │
  │        today … today+HORIZON_DAYS)                         │
  │   2. reduce Cal.com slots → {date: bool} per day           │
  │   3. render availability.json  (dates only, no PII)        │
  │   4. upload to R2 at a STABLE PUBLIC key (not presigned)   │
  └───────────────────────────┬──────────────────────────────┘
                              │ outbound HTTPS (PUT)
                              ▼
             Cloudflare R2  (public bucket / custom domain)
             ├── index.html      ← static page, uploaded once
             └── availability.json ← overwritten each refresh
                              ▲
                              │ client GET (public, cached)
                              │
            client's phone/browser opens the public link,
            JS fetches availability.json, renders month grid
```

Three pieces:

1. **Publisher** (on the box) — a small periodic job that calls Cal.com's **slots** endpoint,
   reduces the result to a per-day boolean over a rolling horizon, and writes/uploads
   `availability.json`. Reuses `calcom_client.py` (add one method) and the R2 client pattern.
2. **`availability.json`** — the data contract (§6). Overwritten every refresh at one stable
   key.
3. **`index.html`** — a static, dependency-light page (§7) uploaded once (re-upload only when
   the page itself changes). It fetches the JSON and renders the calendar. All rendering is
   client-side, so the "server" is just object storage.

### 3.1 Hosting note — public durable objects, NOT presigned URLs

`autoresponder/photos/hosting.py` hands out **short-TTL presigned** R2 URLs for MMS media —
that is the *wrong* pattern here. A client bookmarks/opens this page later, so both objects
must be **durably public** at stable URLs:

- Serve the bucket (or a dedicated prefix) over R2's **public access** — the `*.r2.dev`
  managed domain or, preferably, a **custom subdomain** (e.g. `availability.<domain>`).
- `index.html` and `availability.json` sit at fixed keys; no signing, no expiry.
- Do **not** reuse the presigned `upload()` helper's TTL logic for these; add a small
  `put_public(key, body, content_type, cache_control)` alongside it. Keep the two patterns
  visibly separate so nobody makes the availability page presigned (it would 403 for clients
  after the TTL).

---

## 4. The probe event type (Cal.com setup)

Availability is derived from a **dedicated Cal.com event type** used only as a probe — clients
never book it; the publisher only *reads* its open slots.

- **Name:** e.g. `availability-probe` (internal; not linked publicly).
- **Conflict calendars:** "Check for conflicts" **must include both** the ROVER **and** the
  personal calendar — this is the whole mechanism. Verify both are ticked (Cal.com → the
  event type → Limits/Availability → calendars to check for conflicts).
- **Availability schedule:** a **wide daily window every day** (e.g. **08:00–20:00, 7 days**)
  so that only an *all-day* block (or a wall-to-wall booked day) can zero the day out. A
  narrow window would let one mid-day appointment falsely grey a day (§1 subtlety).
- **Duration / buffers:** short duration (e.g. 30 min), **no buffers**, so a normal day yields
  many slots and the day reliably reads "available." We only care whether the count is
  **> 0**, never the specific times.
- **Do not publicize its booking link.** It exists to be queried, not booked.

> This probe is *separate* from the real drop-off/pick-up/meet-and-greet event types (Addendum
> B §6). Those keep their own schedules/buffers; changing them must not change the probe.

---

## 5. Availability query & reduction

Add to `calcom_client.py` (reuse headers/retry/`TransientCalcomError` machinery already there):

```python
def available_days(self, event_type_id, start_date, end_date, tz):
    """Return {‘YYYY-MM-DD’: bool} — True if Cal.com offers ≥1 slot that day.
    Uses the /v2/slots endpoint for the probe event type; a day with zero slots is
    UNAVAILABLE (all-day block on either calendar, or fully booked)."""
```

- Endpoint: Cal.com **`GET /v2/slots`** (a.k.a. available slots) for `eventTypeId`, ranged
  `start`…`end`, in `CALENDAR_TIMEZONE`. **Confirm the exact v2 param names/response shape at
  build time** — Addendum B already notes Cal.com's response shape drifts across versions, so
  keep the parse defensive (mirror `calcom_client.normalize`'s tolerance).
- **Timezone (Addendum B invariant):** Cal.com timestamps are **UTC** — bucket slots into days
  in `CALENDAR_TIMEZONE` **before** the per-day reduction, or evening slots land on the wrong
  day. Reuse `dates.py` helpers.
- **Reduction:** for each day in the horizon, `available = (slot_count_that_day > 0)`. Days
  Cal.com omits entirely (past days, or beyond its window) are simply absent → the page treats
  absent-future-days conservatively (see §6 `default`).
- **Horizon:** `AVAIL_HORIZON_DAYS` (default **90**) rolling from today. Cal.com may cap how
  far ahead slots are computed; if so, clamp and mark the tail "unknown" rather than "free."

### 5.1 Failure handling (don't publish a lie)

- On `TransientCalcomError` or a partial read, **do not overwrite** the good `availability.json`
  with an empty/all-false one. Keep the last good file; log; let the heartbeat surface repeated
  failure (reuse Addendum B's consecutive-failure alerting ethos).
- `generated_at` in the JSON (§6) lets the page show "as of …" and lets us detect staleness.

---

## 6. Data contract — `availability.json`

Deliberately minimal and **PII-free**:

```json
{
  "generated_at": "2026-09-20T14:05:00Z",
  "timezone": "America/Chicago",
  "horizon_days": 90,
  "default": "unknown",
  "days": {
    "2026-09-20": true,
    "2026-09-21": true,
    "2026-09-22": false,
    "2026-09-23": false
  }
}
```

- `days`: `date → available?`. `true` = at least one open slot; `false` = fully blocked.
- `default`: how the page renders a **future** date not present in `days` (beyond Cal.com's
  computed window): `"unknown"` (neutral, "contact me") — **never** silently "available."
- No names, no event ids, no reasons. A `false` day never says *why* it's blocked.

**Cache-Control:** short (e.g. `max-age=300`) so clients pick up refreshes but R2/CDN still
absorbs load.

---

## 7. Frontend — read-only month calendar (`index.html`)

- **One self-contained static file.** Vanilla HTML/CSS/JS, **no build step, no external CDN**
  beyond what's strictly needed (prefer zero — hand-roll the month grid; it's a solved,
  ~150-line problem). Keeps it robust and reviewable.
- **Layout:** current month grid + prev/next-month arrows (bounded to the horizon). Each day
  cell: **available** = normal/clickable-looking but inert (it's read-only), **unavailable** =
  greyed/struck, **unknown/beyond horizon** = faint "—". A small legend and an "availability as
  of {generated_at}" line. Past days are muted.
- **Copy:** a one-line header in Malik's voice ("Boarding availability — greyed days are full.
  Message me to book."), since there's no booking action on the page.
- **Behavior:** `fetch('availability.json')` (same origin), render, done. Graceful message if
  the JSON can't load ("couldn't load availability — text me and I'll confirm").
- **Mobile-first & accessible:** most clients open it on a phone from a text link; greyed state
  must be distinguishable without relying on color alone (add a strike/label).

**The page is already prototyped** in `availability-front-end-design/` (high-fidelity
`Dog Sitting Calendar.dc.html` + a `README.md` handoff spec + a sample `availability.json`).
Its data contract is **identical to §6** — D1's publisher was verified against it. D3 recreates
that prototype as a plain static `index.html` (its calendar/grid/status logic is directly
portable) and wires `fetch('availability.json')` in place of the embedded sample.

---

## 8. Privacy & security (public surface — the keystone here)

The analog of the auto-responder's approve-and-send keystone: **since this page is public and
un-authed, the invariant is that it can only ever leak a per-day boolean.**

- The publisher emits **only** `{date: bool}` + metadata. Event titles, client names, booking
  refs, and slot *times* never enter `availability.json`. (A blocked day reveals only
  "blocked," not what's on the calendar.)
- **No secrets client-side.** The page is static; the Cal.com API key stays on the box. Clients
  never talk to Cal.com — only to the R2-hosted JSON.
- R2 object for the JSON/HTML is public-read; **write** stays on the box's R2 credentials
  (already in `.env`, gitignored). Confirm the bucket policy grants public **GET** only, not
  LIST (so the bucket can't be enumerated).
- Unguessable-vs-shareable tension: the URL is meant to be shared, so it's public by design;
  that's fine **because the payload is non-sensitive**. This is why §6 keeps it boolean-only.

---

## 9. Refresh cadence & API budget

- All-day blocks change infrequently, so a **periodic refresh every 15–30 min** is plenty
  (`AVAIL_REFRESH_SEC`, default 900). One `/v2/slots` range call per refresh (possibly a few if
  paginated) → trivially within Cal.com limits; far lighter than the 60 s booking poller.
- **Event-driven nudge (optional, phase 2):** re-publish immediately after
  `scheduling.on_booking_confirmed` / `on_booking_cancelled` / `apply_date_change` so a
  just-booked-out day greys within seconds instead of up to 30 min. Cheap and keeps the page
  honest.
- **Where it runs (settled):** a **new background thread inside `rover-sms.service`** (it
  already hosts the Cal.com poller + Telegram poll). No separate unit. `load_dotenv()` at the entrypoint, **absolute
  paths**, and the `netprefs.py` IPv4 preference imported **early** (Addendum B invariant — the
  bridged VM's broken IPv6 stalls Cal.com calls otherwise).

---

## 10. Edge cases

| Case | Behavior |
|---|---|
| Cal.com unreachable at refresh | Keep last good `availability.json`; don't publish all-false; heartbeat alerts on repeated failure (§5.1). |
| Day beyond Cal.com's slot window | Emit as **absent** → page renders `default: "unknown"` ("contact me"), never "available." |
| Single mid-day appointment | Day still has other slots → **available** (that's correct; §1 subtlety). |
| Malik wants a day off | Put an **all-day event** on ROVER *or* personal calendar → zero slots → **unavailable**. This is the manual lever; document it for Malik. |
| Fully booked day (wall-to-wall drop-offs/pickups) | Zero slots → **unavailable** automatically. |
| Timezone / evening slots | Bucket into `CALENDAR_TIMEZONE` before per-day reduction (Addendum B invariant). |
| Probe event type schedule too narrow | Would falsely grey days — call out in setup checklist (§4); verify with a known-free day after setup. |
| Client on a slow/blocked network | Page shows a clear "couldn't load — text me" fallback, not a blank grid. |
| Boarding is multi-night, page shows single days | Acceptable per owner's read-only-grid choice: client eyeballs their span; every night in the span must be non-grey. A future range-checker is a phase-3 option (§12). |

---

## 11. What we are trusting Cal.com for (assumptions to verify at build)

1. `/v2/slots` returns per-day open slots for an event type, computed from its availability
   schedule **and** all "check for conflicts" calendars (ROVER + personal). *(Core assumption —
   verify first with a live probe against a known all-day block on each calendar.)*
2. An **all-day** event on a checked calendar zeros that day's slots. *(Verify on both the ROVER
   and the personal calendar — the personal-calendar half is the new reliance.)*
3. Slot computation honors a horizon of ~90 days (or tells us its cap). *(Verify; clamp if
   smaller.)*
4. UTC timestamps, as Addendum B already established. *(Reuse existing tz handling.)*

> **Build must start by validating #1 and #2 with a real probe** (a `live_availability_test.py`
> alongside `live_scheduling_test.py`, run deliberately, not in the suite) before any frontend
> work — the whole design collapses if Cal.com won't report cross-calendar all-day blocks as
> zero slots.

---

## 12. Build phases

- **D0 — Prove the mechanism.** ✅ **Harness built** — `live_availability_test.py` (deliberate
  run, not in the suite) queries the slots endpoint (defensive across v2 shapes), reduces to a
  per-day count, and prints AVAILABLE/BLOCKED per day. Config keys added to `config.py`
  (`AVAIL_*`). **⏳ Live validation pending on Malik:** create the probe event type (§4) with
  **both** calendars in conflict-checking, set `AVAIL_PROBE_EVENT_TYPE_ID` in `.env`, then run
  the docstring procedure — add an all-day block on a free day, first on **ROVER** then on the
  **personal** calendar, and confirm each flips that day to 0 slots. **Gate:** nothing else
  proceeds until both halves flip to zero.
- **D1 — Publisher.** ✅ **Built.** `calcom_client.available_days()` + defensive
  `slots_by_day()` reducer; `autoresponder/availability/publisher.py` (`refresh` / `run_once`
  / `run_loop` / `start_thread`) writes `availability.json` atomically in the exact frontend
  contract; self-healing (keeps last good feed on a Cal.com outage). Wired into
  `sms_main.py` as a guarded daemon thread (no-ops without `AVAIL_PROBE_EVENT_TYPE_ID`).
  Tests: `tests/test_availability.py` (12, green) — shape-robust reduction, tz day-bucketing,
  the JSON contract, and the don't-publish-a-lie outage path. **Confirmed:** the published
  shape matches `availability-front-end-design/` (the prototyped page + `README.md` contract).
- **D2 — R2 public hosting.** `put_public()` helper; bucket public-GET policy; upload
  `availability.json` on each refresh. Confirm a browser can fetch it at the public URL.
- **D3 — Frontend.** `index.html` month grid; upload once; test on mobile; color-blind-safe
  greying; failure fallback.
- **D4 — Event-driven nudge (optional).** Re-publish on booking confirm/cancel/modify.
- **Phase 3 (later, optional):** a range checker ("enter drop-off/pick-up, get yes/no for the
  whole span") — explicitly out of scope now per owner's read-only-grid choice.

---

## 13. New config / env (to add in `config.py` / `.env.example`)

| Key | Purpose | Default |
|---|---|---|
| `AVAIL_PROBE_EVENT_TYPE_ID` | Cal.com probe event type queried for slots | — (required) |
| `AVAIL_HORIZON_DAYS` | how many days ahead to publish | 90 |
| `AVAIL_REFRESH_SEC` | refresh interval | 900 |
| `AVAIL_R2_PUBLIC_KEY_JSON` | R2 key for `availability.json` | `availability/availability.json` |
| `AVAIL_R2_PUBLIC_KEY_HTML` | R2 key for `index.html` | `availability/index.html` |
| `AVAIL_PUBLIC_BASE_URL` | public base (r2.dev or custom domain) | — (required) |

Reuses existing `R2_*`, `CALCOM_API_KEY`, `CALENDAR_TIMEZONE`. No new secrets beyond the probe
event-type id and the public base URL — both non-sensitive.

---

## 14. Decisions (settled 2026-09-20)

1. **Custom domain — yes.** The public page lives at a **custom subdomain**
   (`availability.<yourdomain>`), not `*.r2.dev` — reads as trustworthy in a text. Point the
   subdomain at the R2 bucket (Cloudflare custom domain on the bucket). `AVAIL_PUBLIC_BASE_URL`
   is that subdomain.
2. **Publisher home — inside `rover-sms.service`.** Runs as a **background thread** in the SMS
   service (which already hosts the Cal.com poller + Telegram poll). No separate timer/unit.
3. **Horizon — 90 days.** `AVAIL_HORIZON_DAYS = 90`.
4. **Beyond-horizon days — "contact me to check."** Days past Cal.com's computed window render
   as `unknown` with a **"contact me to check"** message (never blank, never "available").
