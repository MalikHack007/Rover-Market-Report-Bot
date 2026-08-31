# Rover Auto-Responder — Design Addendum C: Photo-Update Assistant

**Version:** v0.1 (design)  ·  **Status:** P1–P3 built (2026-08-31); not yet live-tested
end-to-end (see §11 table + §12)  ·  **Date:** 2026-08-28
**Depends on:** base design (v0.3), Addendum A (SMS transport & approve-and-send),
Addendum B (calendar). Read the nested `CLAUDE.md` first — especially the
**"Outbound MMS / photo updates — policy exception"** section, which this addendum implements.

---

## 1. Purpose

Give Malik a fast way to send **on-brand photo updates** to the owners of dogs currently in
his care. He dumps photos into Telegram and tags each by the dog's name; the bot writes a
warm caption in his voice; he edits any caption or fixes a mis-tag; and then with **one
Send-all tap** the bot sends every **photo + caption as an MMS** to each booking's owner — one
by one, automatically — from Malik's own phone number so they land in the existing Rover
conversations. Nothing sends before that single tap.

This is a **screening-independent** feature: it operates only on **confirmed, currently-active
bookings**, and it is the **second sanctioned message type on a converted thread** (alongside
the Addendum B scheduling-links card). It never drafts screening replies and never touches
the drafter's stage machine.

---

## 2. Transport (already proven — see the PoCs)

The hard part — getting a full-size image to the owner — was validated end-to-end this cycle:

- **Send = Telerivet** (`telerivet_poc.py`). The Telerivet Gateway app on the Pixel sends an
  MMS **from Malik's own number**; Rover's relay forwards it to the owner **in-thread**
  (verified: a real full-size dog photo arrived). Endpoint
  `POST /v1/projects/<id>/messages/send`, Basic auth (API key as username), `media[]` array
  of public URLs. **httpSMS was rejected** — it caps attachments at a few KB
  (`mms_poc.py`, kept only as the record of that finding). **Telerivet is configured send-only**
  — incoming-message forwarding is turned OFF in the app so inbound texts don't consume the
  50/day quota (inbound is owned by "SMS Gateway for Android"). See §15.
- **Hosting = Cloudflare R2** (`r2_upload.py`) — **decided; this is the approach.** Telerivet
  needs a **public URL** to fetch the media, so each photo is uploaded to R2 and referenced by a
  **short-TTL presigned URL** (default 1h). Before upload we **bake in EXIF orientation** (fixes
  MMS transcode rotation); photos are otherwise **kept at their original size — no downscaling or
  recompression** (Malik's decision). R2 is free at this volume.
- **Read (photo intake) = Telegram.** Photos arrive in the existing Telegram chat the bot
  already polls — no MMS-in, no new inbound channel.

**Policy note:** this deliberately breaks the "no third-party cloud relay" rule for the MMS
path only (Telerivet cloud + FCM + R2); the SMS **text** path stays fully local on "SMS
Gateway for Android." That exception is recorded in `CLAUDE.md` and was accepted by Malik.

### 2.1 Constraints that shape the design

- **Telerivet plan:** **50 messages/day** and **200 API calls/day**, reset daily. One photo
  update = 1 send call (+ optional status checks). Comfortable for a dog boarder, but the
  pipeline must **count against a daily budget** and refuse/queue gracefully near the cap.
- **Carrier data:** **2 GB/month** — MMS rides cellular data, and **photos are sent at full
  size by decision** (no downscaling). A multi-MB photo therefore uses meaningfully more of
  the cap, so at full size the **data budget can bind before the 50/day send cap** — track data
  use, not just send count. (Note: carriers usually transcode MMS down at delivery regardless,
  so the recipient may still receive a compressed image even though we send the original.)
- **Presigned-URL TTL must outlast the fetch.** Telerivet/the phone fetches the media
  asynchronously after the API accepts the send; the URL must still be valid then (1h is
  ample). Objects auto-delete via a bucket lifecycle rule (privacy: client dog photos should
  not linger publicly).

---

## 3. End-to-end flow

```
Malik starts a photo-update session (a Telegram entry / command) — no typing, no captions
   │
   ▼
[Roster]  bot posts the dogs-in-custody as a TAP keyboard: [ Blue · Anika ] [ Max · Sara ] ...
   ▼
[Assign]  Malik TAPS a dog -> it's the "active dog"; he then just sends that dog's photo(s),
   │  one or many. Each `photo` is downloaded (getFile), stashed, and appended to that dog's
   │  pending update. Tap the next dog, send theirs, repeat. NO name typing; MULTIPLE photos
   │  per dog accumulate under the active dog.
   ▼
[Caption] a line is auto-PICKED from the pre-written pool per dog, {pet} substituted (§7)
   ▼
[Review]  Malik taps ✅ Review -> one card per dog: names the OWNER, shows the dog's photo(s) +
   │       caption, PER-DOG buttons only: ✏️ Edit caption · 🔁 Another caption · ➕ More photos ·
   │       ⏸ Hold · 🗑 Discard (NO per-dog send button). Plus a summary card: ONE ✅ Send all (N).
   ▼
[Send all] Malik taps ✅ Send all ONCE (THE GATE — nothing sends before this). The bot then
   │        dispatches every READY dog-update sequentially, on its own:
   │        for each -> claim idempotently -> R2 upload+presign EACH photo (oriented, orig size)
   │        -> Telerivet send to the relay number with ALL the dog's media URLs -> record id
   ▼
[Confirm] per-update Telerivet delivery status -> sent/delivered/failed; a running progress
   │        summary ("Sent 5/6 · 1 failed"); failures alerted + retryable (§8.3)
   │        R2 objects lifecycle-delete after ~1 day
```

Malik assigns and reviews **per dog** (by tapping, never typing), edits any caption, then
**sends the whole batch with one tap** — he does not approve each dog. The per-dog cards are the
same shape as the Addendum A draft card (target-labeled, delivery-confirmed); the single
Send-all tap is the explicit approval covering the reviewed batch. The safety keystone is
reused — batch-approved, not per-item (§8).

---

## 4. Scope

**In scope (v0.1):** start a photo-update session; **tap a dog from the roster, then send that
dog's photo(s)** — no name typing, **multiple photos per dog**; auto-**pick** a pre-written
caption (editable, re-rollable — no LLM); **review the whole batch and send with one tap**
(sequential, per-dog delivery-confirmed); daily-budget + data awareness; orientation fix
(original size) + teardown.

**Out of scope (v0.1, revisit later):** **private bookings** — Malik sends those updates
manually from his personal number for now, so the roster is **Rover bookings only** (§5);
scheduled/auto-cadence updates ("send everyone a photo at 5pm"); video/other attachment types;
LLM-written or photo-aware captions (v0.1 uses a fixed pool).

---

## 5. The roster — "dogs in my custody right now"

The set of valid targets is derived, not maintained by hand. A dog is **in custody today** if
its **Rover** booking's stay covers the current date:

- `status = 'converted'` **and** `has_booked = 1` (Rover bookings only);
- **and** `stay_dates` brackets today, **inclusive**: `drop-off ≤ today ≤ pick-up`. Inclusive
  bounds already cover a same-day drop-off (start == today) and same-day pick-up (end == today);
  **no extra grace window** — a ±1-day margin wrongly pulled in stays that start tomorrow or
  ended yesterday.

**Private bookings are excluded** — Malik handles those photo updates himself from his personal
number for now (revisit later; §16). All Rover bookings already live in the database, so the
roster is complete without any manual entry. Pay-first bookings that only ever arrived by email
still have a `thread_key` (the normalized phone from the confirmation email — see Addendum A
email pipeline), so they appear on the roster too.

Each roster entry resolves to: **owner name, pet name, and the target relay number**
(`thread_key` = the conversation phone number, which is what Telerivet sends to).

A `store.list_active_bookings(today)` helper returns this list; it's the source for the tap
keyboard (§6) and the daily-budget display.

---

## 6. Assigning photos to dogs — by tapping, never typing

Labels are human-supplied, but **Malik never types a dog's name**. The mechanism is a
**tap-to-set-active-dog** flow:

1. The bot posts the roster (§5) as an **inline tap keyboard**, one button per dog in custody
   (`<Pet> · <Owner>`).
2. Malik **taps a dog** → it becomes the **active dog** (session state, `photo_active:<chat>`).
3. He then **sends that dog's photos** — one or several. Every incoming `photo` while a dog is
   active is downloaded and **appended to that dog's pending update**. Multiple photos per dog
   just accumulate.
4. To switch, he taps a different dog on the (still-visible) roster keyboard and sends theirs.
5. When done, he taps **✅ Review** to generate the per-dog cards (§8).

This is the whole labeling model: **tap a name, dump photos, tap the next name.** No captions to
type, no name-matching to get wrong. A photo that arrives with **no active dog set** is parked
and the bot re-shows the keyboard ("Tap who this is of first").

Fixing a mistake: each review card has a **🏷 Re-assign** button that reopens the tap keyboard
and moves that dog's photos to the chosen dog. **➕ More photos** re-arms that dog as active so
he can add to it.

Every valid target is already on the roster (all Rover bookings are in the DB), so there is **no
"unknown dog" path** in v0.1 — a dog not in custody simply isn't shown. (Private clients, who
reach Malik on his personal number, are handled manually for now — see §16.)

---

## 7. Caption selection — a pre-written pool, no LLM

**No LLM call.** Each dog's update gets a caption **picked from a long array of pre-written
lines**; there is no vision model, no generation, no token cost.

- **Source:** `captions.txt` — one caption per line, gitignored (with a committed
  `.example`), editable without code changes. Lines may contain a `{pet}` placeholder
  (e.g. `"{pet} is having the best time here! 🐾"`, `"Someone made a new friend today 🐶"`),
  substituted with the dog's name at pick time. That's the only "personalization" — no LLM.
- **Selection:** pick pseudo-randomly, **avoiding immediate repeats to the same client** — a
  per-thread "last caption index" (or a small recently-used ring) so an owner doesn't get the
  same line two updates running. A photo-less pool this size makes the messages feel varied
  without any model.
- **Per dog, not per photo:** one caption accompanies that dog's whole update, however many
  photos it carries.
- **Editable / re-roll:** the card's **✏️ Edit caption** stores Malik's own wording as the
  pending text (exactly the Addendum A edit path); **🔁 Another caption** picks a different
  line from the pool. Send-all sends whatever text is pending.

**Cost:** effectively **$0** — the caption path makes no API calls. The only running costs are
Telerivet's per-message fee and R2 hosting (free at this volume). This also means the feature
has **no dependency on `ANTHROPIC_API_KEY`** (the screening drafter still uses it — separate
path).

---

## 8. Review, then batch-send — the safety keystone (reused, batch-approved)

Malik reviews the whole set, edits anything he wants, then sends **the entire batch with ONE
tap** — he does **not** approve each dog individually. This is still fully human-gated: the
single **✅ Send all** tap is the explicit approval, and it covers a set he has just reviewed
dog by dog. **Batch approval ≠ unattended sending** — a human read every caption + target and
pressed one deliberate button; there is no auto-send path, and none may be added. All Addendum A
keystone guarantees hold, **per dog-update** (the send unit is one dog's update, carrying all of
that dog's photos):

- **Nothing sends without the explicit Send-all tap.** No unattended/auto path exists.
- **Unmistakable targets:** each per-dog card names the **owner** (and pet) it will reach, and
  the Send-all button names the **count of dogs** ("Send updates to all 6 owners"). Review is
  per dog; the approval is one tap.
- **Idempotent per dog-update:** each `photo_updates` row is claimed before its send, guarded
  per `(thread_key, episode, batch_id)` — re-tapping Send-all, or a retry, cannot resend a
  dog-update that already went out.
- **Delivery-confirmed per dog-update:** Telerivet status flips each row to
  sent/delivered/failed; a failure **alerts** and stays retryable — no approved update silently
  vanishes.

### 8.1 The review view and what "Send all" does

Each dog's review shows the **actual photos** followed by a control card:

- **Photo preview** — the dog's photos are shown back by their existing Telegram `file_id`s (no
  re-upload): one photo via `sendPhoto`, several as a `sendMediaGroup` album (Telegram caps an
  album at 10, so extra photos spill into further albums). This is purely visual — albums can't
  carry buttons — so it sits directly above the control card.
- **Control card** — one text card per dog with per-dog controls only: ✏️ Edit caption,
  🔁 Another caption, ➕ More photos (re-arm this dog as active), ⏸ Hold (exclude from this
  batch), 🗑 Discard — and **no per-dog send button**. It names the owner + pet + photo count and
  shows the caption. Editing a caption (reply to the card) or re-rolling only edits this text
  card; the photos above are left as sent.

A separate **batch summary card** carries the single **✅ Send all (N)** button, where N is the
count of `ready` dog-updates. Send-all sends whatever caption is pending on each.

On the Send-all tap, the bot dispatches the batch **sequentially, on its own**, for every
`ready` (non-held, non-discarded) dog-update:

1. Claim the dog-update (idempotency; already-sent or racing ones are skipped, not resent).
2. **Orient + upload EACH of that dog's photos** to R2 (EXIF orientation only — original size
   kept), collecting a short-TTL presigned URL per photo.
3. **Send ONE MMS** via Telerivet to the dog's relay number (`thread_key`), caption as
   `content` and **all the dog's URLs in `media[]`**; store the returned `telerivet_msg_id`.
   A multi-photo update is therefore **one Telerivet message = one send against the 50/day cap**
   (not one per photo).
4. **Pace** to respect the Telerivet rate and daily budget (§8.2); **continue on a per-dog
   failure** rather than aborting the batch.
5. Post a **running progress summary**, then a final result line ("Sent 5/6 · 1 failed —
   retry?"). Failed dog-updates stay `ready`/`failed`; another Send-all sends **only** the
   unsent ones. Held/discarded ones never send.

A **batch** is the set of dog-updates from the current session (grouped by `batch_id`, §10); a
new session starts a new batch.

> **Multi-image is verified (2026-08-29 PoC):** 4 full-size images in one `media[]` MMS
> delivered as a single message, none missing, correctly oriented, and counted as **one
> Telerivet message** against the 50/day cap. So one-MMS-per-dog is the confirmed path. The
> exact ceiling above 4 wasn't probed; as a **defensive-only** guard, if a very large combined
> MMS is ever rejected, fall back to a short **burst** of MMS for that dog (each counting
> against the 50/day cap). This fallback is not expected to fire at realistic per-dog counts.

### 8.2 Daily budget

The pipeline tracks a per-day counter (meta key `mms_sends:<YYYY-MM-DD>`) against the Telerivet
**50/day** cap (and keeps API calls under 200/day). On the **Send-all** tap the batch size is
checked against the remaining budget **up front**: if it fits, the whole batch sends; if it
exceeds the cap, Send-all **warns and sends what fits (oldest-first)**, leaving the rest `ready`
for tomorrow — never failing silently. Status-check calls are batched to stay within the 200
API-calls/day budget. **Because photos go full-size**, the pipeline also accumulates sent bytes
for the month (meta key `mms_bytes:<YYYY-MM>`) and warns as it approaches the **2 GB carrier
cap**, since a run of large photos can hit that before the send count.

### 8.3 Delivery confirmation — batched polling within the API budget

The box takes no inbound webhook (home NAT), so delivery status is **polled** — but polling is
designed to stay well inside Telerivet's **200 API-calls/day** budget:

- **One query covers ALL outstanding messages, not one per message.** A single
  `GET .../messages?direction=outgoing&status=queued` (Telerivet supports status filters and a
  `count=1` mode) returns every still-pending send in one call. With the 50/day message cap, all
  outstanding fit on one page → **one poll = one API call**, regardless of batch size.
- **Poll only while something is pending.** The worker runs only after a Send-all, over that
  batch's not-yet-terminal `photo_updates`; when none are pending it makes **zero calls** (it is
  not a clock-driven background loop).
- **Adaptive back-off + give-up window.** Poll ~1 min after the send, backing off (2, 5 min);
  stop the instant all reach a terminal status (`delivered`/`failed`); after ~15–30 min mark any
  stragglers `sent (delivery unconfirmed)` rather than polling forever (MMS receipts are flaky).
- **Reserved send budget (hard guardrail).** A daily counter (meta `telerivet_api_calls:<date>`)
  reserves headroom for sends; if polling would cross the reserved ceiling it **stops and falls
  back to `sent (unconfirmed)`** — sends always win the budget, so 200/day is never hit.

Budget in practice: sends ≤50 calls; batched polling adds only tens; even **~30 client-updates
in a day lands around ~70 of 200 calls**. A `failed` status alerts Malik; the poller is
self-healing — a missed poll is corrected by the next.

---

## 9. Outbound MMS mechanics (building blocks proven)

- **`r2_host`** (productionizes `r2_upload.py`): `upload_and_presign(path)` bakes in EXIF
  orientation only and keeps the **original size** — upright photos upload byte-for-byte
  untouched; only rotated ones are re-encoded (at `R2_IMAGE_QUALITY`, default 95). Returns a
  presigned GET URL. A lifecycle rule on the bucket auto-deletes objects after ~1 day; we may
  also delete eagerly after `delivered`.
- **`telerivet_client`** (productionizes `telerivet_poc.py`): `send(number, text, media_urls)`
  → `telerivet_msg_id`, where `media_urls` is a **list** (one dog-update = one MMS carrying all
  its photos in `media[]`); `status(msg_id)` → delivery state. Basic-auth, `phone_id` selects
  the Pixel gateway so it sends from Malik's number.
- **Data budget is by send-count and byte-size**, not enforced by shrinking: since photos go
  full-size, the pipeline watches the **2 GB/mo data** use alongside the 50/day count (§8.2),
  because a run of large photos can hit the data cap first.

---

## 10. Data model (SQLite)

Two new tables (a dog-update + its photos); confirm final columns at build time in
`models.py`/`store.py`.

```
photo_updates                        -- one row per dog per session (the send unit)
  id             INTEGER PK
  batch_id       TEXT      -- groups one session's dog-updates; Send-all acts on a batch
  thread_key     TEXT      -- the owner's conversation/relay number (target)
  episode        INTEGER   -- which booking (Addendum A episodes)
  pet_name       TEXT      -- snapshot, for the card + caption
  caption        TEXT      -- pending/approved caption (picked from the pool; editable)
  status         TEXT      -- collecting | ready | held | sent | delivered | failed | discarded
  telerivet_msg_id TEXT
  created_at, updated_at TEXT
  UNIQUE (thread_key, episode, batch_id)   -- one dog-update per dog per batch (idempotency)

photo_update_media                   -- one row per photo attached to a dog-update
  id             INTEGER PK
  update_id      INTEGER   -- FK -> photo_updates.id
  telegram_file_id TEXT    -- source photo (Telegram); dedupes a redelivered photo
  local_path     TEXT      -- staged original on the box
  r2_key         TEXT      -- object key once uploaded (for teardown)
  position       INTEGER   -- order within the update
  UNIQUE (update_id, telegram_file_id)
```

Send-all operates on all `ready` rows of the **current `batch_id`**; `held`/`discarded` are
skipped. `held` is a toggle (⏸) that excludes a dog-update from this batch without deleting it.
Adding photos to the active dog appends `photo_update_media` rows to its (still `collecting`)
`photo_updates` row.

- The **card ↔ photo_update** mapping reuses the existing `cards` table pattern (a Telegram
  message id → the row it edits), so replying to a card edits *that* caption.
- **Meta keys:** `mms_sends:<date>` (daily send count), `mms_bytes:<month>` (monthly data use,
  §8.2), `photo_batch:<chat>` (the current open batch), `photo_active:<chat>` (the currently
  tapped/active dog photos attach to, §6), `caption_last:<thread>` (last caption index, to avoid
  immediate repeats, §7).
- Reuse of `sends` (Addendum A) is possible but that ledger is text-keyed; the dedicated tables
  above carry the media/target/status a photo update needs.

---

## 11. Modules & where logic lives

Feature code lives in its **own subpackage `autoresponder/photos/`** so it doesn't clutter the
top-level package next to the SMS/email/calendar modules. Only two tiny touchpoints reach into
existing shared files (the store-init hook and the Telegram photo dispatch).

| Concern | Module | Status |
|---|---|---|
| Feature settings (Telerivet / R2 / captions / budgets) | `photos/config.py` | ✅ built |
| R2 upload + presign + orient (original size) | `photos/hosting.py` (from `r2_upload.py`) | ✅ built |
| Telerivet send + batched status query | `photos/telerivet.py` (from `telerivet_poc.py`) | ✅ built |
| Caption **pick** from pool + `{pet}` (no LLM) | `photos/captions.py` (+ `photos/captions.txt`) | ✅ built |
| `photo_updates` / `photo_update_media` schema + CRUD + roster + budget/session meta | `photos/store.py` (shared conn+lock; hooked into `store.init_db`) | ✅ built |
| Roster of dogs in custody (**Rover only**) | `photos/store.list_active_bookings()` (parses `stay_dates`, inclusive `start ≤ today ≤ end`, no grace) | ✅ built |
| Telegram rendering (`ph:*` callbacks, keyboards, cards), **photo preview** (`sendPhoto`/`sendMediaGroup` by file_id) + `getFile` download | `photos/telegram.py` (reuses `telegram_notify` primitives) | ✅ built |
| Tap-to-assign (active dog), multi-photo accumulate, re-assign, review cards | `photos/pipeline.py` | ✅ built |
| Callback router, caption edit/re-roll, **batch Send-all**, 50/day budget guardrail | `photos/approve.py` (mirrors `sms_approve.py`) | ✅ built |
| Photo intake + callback/text routing (`ph:*` → photos, else SMS) | `telegram_poll.py` (+`on_photo`) and `sms_main.py` (routing) | ✅ built |
| Batched delivery-status poller (poll-only-while-pending, give-up window, reserved budget) | `photos/poller.py` (thread in `rover-sms`) | ✅ built (P2) |
| Eager R2 teardown + local-file cleanup after `delivered`/`failed`/give-up | `photos/poller.py` `_teardown()` / `hosting.delete()` | ✅ built (P2) |

**Service placement:** everything runs inside **`rover-sms.service`**, which already owns the
single Telegram poller and the Telerivet-adjacent background workers. No new systemd unit; the
delivery-status poller is one more daemon thread there. The email-fallback service is
untouched.

---

## 12. Phasing

- **P1 — core loop (✅ built):** tap-to-assign roster → **accumulate multiple photos per dog** →
  pool caption (pick + `{pet}`) → per-dog review cards with photo preview → **one Send-all** →
  R2 upload each photo → **one MMS per dog** with all its media. Daily-send budget guard.
- **P2 — delivery confirmation (✅ built):** batched Telerivet status poller
  (`photos/poller.py`) — poll-only-while-pending, adaptive give-up window, reserved API budget;
  flips rows to `delivered`/`failed`, alerts on failure, and tears down R2 objects + local files
  on finalization. (Multi-image-in-one-MMS is verified, so the burst fallback is a defensive-only
  guard, not planned work.)
- **P3 — polish (✅ built 2026-08-31):** roster niceties — dogs that already got an update
  today are prefixed **✅** on the tap keyboard (`store.threads_updated_today`, counts only
  `sent`/`delivered`); Telegram **album intake** — an album's photos share one
  `media_group_id`, threaded through `dispatch_update → on_photo`, so the "tap a dog first"
  nudge and the per-dog "collecting" ack each fire **once per album**, not once per photo
  (all photos still attach to the active dog); **"same caption for all"** — a summary-card
  button (shown when >1 dog) picks one pool line and applies it to every dog-update, each
  still personalizing `{pet}`, re-rendering every card (reverse card map added to
  `store.link_card`). Tests: `test_photos_p3.py`.

---

## 13. Config / secrets (`.env`)

Already scaffolded in `.env.example`:

- `TELERIVET_API_KEY` (secret), `TELERIVET_PROJECT_ID`, `TELERIVET_PHONE_ID`.
- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID` (secret), `R2_SECRET_ACCESS_KEY` (secret), `R2_BUCKET`,
  `R2_PRESIGN_TTL`, `R2_IMAGE_QUALITY` (re-encode quality for rotated photos only).
- Caption pool path (new): `CAPTIONS_PATH` → `captions.txt` (like `PLAYBOOK_PATH`). **No
  caption model** — the pool is used verbatim, so no `ANTHROPIC_API_KEY` is needed for this
  feature (screening still uses it, separately).
- New deps already in `requirements.txt`: `boto3`, `Pillow`. (No new LLM dependency.)

Secrets stay gitignored; only `.example` copies are committed. `load_dotenv()` at every
entrypoint; absolute paths under systemd.

---

## 14. Hard-rule compliance

- **No unattended sending.** The batch goes out only on Malik's explicit **Send-all** tap,
  covering the dog-updates he reviewed dog by dog. Batch approval is still human approval — one
  deliberate tap over a reviewed set — not an auto-send path. No unattended path exists or may
  be added; the keystone is reused, not re-implemented.
- **Don't automate Rover's web platform.** Delivery is over SMS/MMS to the relay number — the
  same sanctioned channel as text replies — never the Rover website or app. Photos are **not**
  posted into Rover's own photo-update surface.
- **Secrets never committed; IPv4 preference intact; SQLite lock intact.** Unchanged.

---

## 15. Non-obvious invariants & risks (carry into the build)

- **Photos are sent full-size — never downscale or recompress to save space.** The only
  transform allowed is baking in EXIF orientation (below); upright photos upload byte-for-byte.
  Because of this, **watch the 2 GB/mo data use**, which can bind before the 50/day send count.
- **Presigned TTL must outlast Telerivet's async fetch** (1h default is safe); too-short TTLs
  fail intermittently and confusingly.
- **Bake in EXIF orientation** before upload — MMS transcoding strips metadata and otherwise
  rotates portrait photos (observed and fixed in the PoC).
- **Idempotency is per dog-update**, keyed on `(thread_key, episode, batch_id)` — re-tapping
  Send-all or a retry must not resend a dog that already went. A redelivered Telegram photo is
  deduped separately by `photo_update_media (update_id, telegram_file_id)`.
- **One tap sends the batch, but it stays gated.** Send-all is the single explicit approval for
  a set Malik reviewed dog by dog — not an auto-send path. Because idempotency is per dog-update,
  a second Send-all (e.g. after a partial failure) sends **only** the not-yet-sent dogs, never a
  duplicate. Held/discarded dogs are excluded.
- **A multi-photo update is ONE MMS = one send** against the 50/day cap (not one per photo) —
  **verified** with 4 full-size images (2026-08-29 PoC). A very large combined MMS could still
  be rejected/split; the burst fallback (§8.1) is the defensive-only guard for that.
- **Daily caps are real** (50 sends / 200 API calls) — budget and degrade gracefully; never
  fail an Approve silently at the cap.
- **Telerivet is send-only — keep incoming-message forwarding OFF** (verified 2026-08-29).
  Otherwise every inbound text the Pixel receives is forwarded to Telerivet and **counts against
  the 50/day quota** — wasted, since inbound is already owned by "SMS Gateway for Android." This
  is a deploy setting in the Telerivet Gateway app (disable forwarding of incoming messages);
  re-check it if the app is reinstalled or the phone is re-provisioned. Sending is unaffected.
- **Photo updates only on active, confirmed bookings.** Never target an `active`/screening
  thread or a stay that isn't current — the roster query is the guard.
- **Privacy:** a client's dog photo is briefly public at an unguessable presigned URL; keep
  the TTL short and lifecycle-delete the object. Don't retain originals longer than needed.

---

## 16. Open questions (resolve during P1)

1. ~~**Does Telerivet's cloud fetch the media, or the phone?**~~ **CLOSED (2026-08-29): going
   with R2.** A LAN-local URL might have saved the R2 round-trip, but Malik is happy with R2
   (verified, free at this volume), so we're not chasing that optimization. R2 is the approach.
2. ~~**Multi-photo MMS behavior (the main one):**~~ **RESOLVED (2026-08-29 PoC).** Verified: **4
   full-size images in one `media[]` MMS delivered as a single message, none missing, all
   correctly oriented, and counted as ONE Telerivet message** against the 50/day cap. So
   **one-MMS-per-dog holds** and the burst fallback (§8.1) is a defensive-only path, not an
   expected one. The exact ceiling above 4 wasn't probed — realistic per-dog updates are a
   handful of photos, so 4-verified is enough for v0.1; if a future larger batch is ever
   rejected, that's when the burst fallback triggers.
3. ~~**Delivery confirmation / staying within 200 API calls/day:**~~ **RESOLVED.** Polling stays
   well inside the budget via **batched status queries** (one query covers all outstanding, not
   one call per message), **poll-only-while-pending** (zero calls when idle), adaptive back-off +
   a give-up window, and a **reserved send budget** guardrail that stops polling before the cap
   so 200/day is never hit. Full spec in §8.3. Comfortable at up to **~30 client-updates/day**
   (~70 of 200 calls). (Data-cap headroom at that volume is a separate carrier-plan matter, not
   an API-budget one — out of scope here.)
4. **Private bookings (deferred):** currently out of scope — Malik sends those from his personal
   number by hand. Revisit whether to fold them in (they'd reuse the Addendum B `private:` thread
   as the target) if the manual path becomes a chore.

---

*Building blocks proven this cycle:* `telerivet_poc.py` (MMS send, real photo delivered),
`r2_upload.py` (hosting + orientation, original size), and the rejected `mms_poc.py` (KB cap).
This addendum turns those into the approve-gated, roster-driven feature above.
