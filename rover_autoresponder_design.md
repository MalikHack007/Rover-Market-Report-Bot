# Rover New-Client Auto-Responder — Design Document

**Status:** Draft v0.3 (design review — no code yet)
**Owner:** Malik
**Last updated:** 2026-07-27
**Repo (proposed):** extend `/home/malikhack007/Repos/rover-automations`

**Changelog v0.2 → v0.3:** Trigger committed to **Gmail push (watch + Pub/Sub) from the start** — fast-poll demoted to a break-glass fallback only. FAQ doc **deferred** — pluggable input added later; until then, uncovered S3 questions flag to you. Runs **24/7, no overnight suppression**. Added downtime-resilience note (Pub/Sub retention + Gmail history replay). Remaining Decisions (§12) replaced with a Decisions Log.

**Changelog v0.1 → v0.2:** Read side fixed to email-only (notification emails confirmed to carry full message text); Playwright fallback removed. Dedicated Gmail account for Rover messages (isolated from the primary/report account). Availability and pricing removed from drafting (availability always assumed; clients already see pricing at request time). Scope expanded to full multi-turn screening flow with `converted` / `not_suitable` terminal states. Near-real-time trigger via Gmail push added. Conversation state machine (§4) added from the response playbook (Appendix A).

---

## 1. Problem & Context

New-client inquiries on Rover need fast, consistent, on-brand replies that walk a prospective client through your screening flow (greeting → questionnaire → services/policy → meet-and-greet or video call). Doing this by hand is slow and repetitive, and you want to respond **ASAP**.

Rover actively fights automated traffic, so **we never post through Rover**. The system *drafts* a reply and hands it to you via Telegram; you copy-paste it into Rover manually. The manual send keeps your account out of any automated-posting detection surface entirely.

The read side is solved by reading **your own email**: Rover sends a notification email per client message, and (confirmed from a real sample) the email body contains the full message text. So there is no Rover scraping anywhere in this system.

---

## 2. Goals & Non-Goals

**Goals**
- Trigger on a new client message **the moment** its notification email arrives (Gmail push), running **24/7 with no quiet hours**.
- Detect which stage of the screening conversation the thread is in, and draft the stage-appropriate reply in your voice.
- Keep drafting replies to follow-ups until you mark the thread **converted** or **not suitable**.
- Deliver each draft to Telegram formatted for one-tap copy, with action buttons.
- Never auto-send anything to Rover. Never touch Rover programmatically at all.
- Reuse existing infra (Gmail API, `.venv`, systemd user services, linger).

**Non-Goals (v1)**
- No auto-posting to Rover.
- No availability logic — availability is **always assumed** (you decide suitability through the screening process, not the bot).
- No pricing in drafts — clients already see your rates when they send a request.
- No handling of post-booking / mid-stay logistics (e.g. "I'm tracking for 3:30"). Once a thread is `converted`, the bot stops drafting; off-playbook messages are flagged, not answered.
- No fine-tuning — few-shot prompting only (§7).

---

## 3. High-Level Architecture

```
        ┌───────────────────────────────────────────────┐
        │  Rover  (never written to by automation)       │
        └───────────────┬───────────────────────────────┘
                        │ emails a notification per client message
                        ▼
        ┌──────────────────────────────┐
        │  Dedicated "Rover-msgs" Gmail │  separate account from your
        │  account (read-only OAuth)    │  primary / rover_report.py email
        └───────────────┬──────────────┘
        push: watch + Pub/Sub  │  (primary; poll = break-glass, §5.1)
                               ▼
   ┌──────────────┐   ┌──────────────────┐   ┌──────────────────┐
   │ Inbox Watcher│──▶│ Parser + Deduper │──▶│ State Store      │
   │              │   │ owner, pet,      │   │ (SQLite)         │
   │              │   │ dates, msg text  │   │ thread → stage   │
   └──────────────┘   └────────┬─────────┘   └──────────────────┘
                               │ new message + thread's current stage
                               ▼
                      ┌──────────────────┐
                      │ Stage Resolver   │  which stage is this thread in?
                      │ + Context        │  load playbook + FAQ +
                      │                  │  client-side thread history
                      └────────┬─────────┘
                               ▼
                      ┌──────────────────┐
                      │ LLM Drafter      │  Claude (Anthropic API):
                      │                  │  classify stage, draft reply,
                      │                  │  flag if off-playbook
                      └────────┬─────────┘
                               ▼
                      ┌──────────────────┐        ┌───────────────┐
                      │ Telegram Bot     │◀──────▶│ You           │
                      │ draft + buttons  │  copy  │ paste → Rover │
                      └──────────────────┘        └───────────────┘
                        buttons: Mark sent (→advance stage),
                        Regenerate, Tone, Converted, Not suitable
```

**Runtime shape:** one long-running systemd **user service**. The Telegram bot must stay up to receive button callbacks, and (with push) the Gmail subscription streams events into the same process.

---

## 4. Conversation State Machine

Your response playbook (Appendix A) defines a staged screening flow. The bot tracks each thread's `stage` in SQLite and advances it when you tap **Mark sent**. On each incoming message, the LLM is told the current stage and drafts the next step — or flags if the message doesn't fit.

```
 [S0 INITIAL_INQUIRY]  client's opening ("are you available to sit X from _ to _?")
   │  draft: "Hey [owner], [dog] looks adorable! …happy to potentially watch your
   │         pup… mind answering a few quick questions to make sure we're a good fit?"
   │  (Mark sent) ▼
 [S1 AWAITING_CONSENT]
   │  client "sure!"/"yes"   ──▶ draft: 6-question questionnaire + "About My Services"
   │                              no-entry-into-home policy blurb
   │  client hesitant/other  ──▶ FLAG to you
   │  (Mark sent) ▼
 [S2 AWAITING_ANSWERS]
   │  client answers the Qs  ──▶ draft: "Thanks for the quick & comprehensive answers!
   │                              I'd be happy to take care of [dog]… Any questions for
   │                              me?"  (personalized to their answers)
   │  (Mark sent) ▼
 [S3 POST_SCREEN]
   │  meet-and-greet ask     ──▶ draft: Brownie Neighborhood Park + link + "next 7 days?"
   │  insists on the house   ──▶ draft: driveway-intro compromise
   │  live video call ask    ──▶ draft: "what time today works?"
   │  covered question       ──▶ draft answer from playbook / FAQ
   │  uncovered question     ──▶ FLAG to you
   │
   ├──(you tap Converted)────▶ [CONVERTED]     bot stops drafting this thread
   └──(you tap Not suitable)─▶ [NOT_SUITABLE]  bot stops drafting this thread

 Any stage — off-playbook or post-booking logistics
   (e.g. the "I'm tracking for 3:30" / "Here" messages during a confirmed stay)
   ──▶ FLAG to you, no auto-draft.
```

The classifier's job each turn: (a) confirm the message fits the expected next step, (b) if it's a covered side-question, answer it without losing the thread's place, (c) if it's off-playbook, flag rather than guess.

---

## 5. Components

### 5.1 Inbox Watcher

Watches the **dedicated Rover-messages Gmail account** for new Rover notifications via **Gmail push, from the start** (your call):

- `users.watch()` registers the mailbox against a Cloud Pub/Sub topic; Gmail publishes a tiny event within seconds of mail arriving.
- The service holds a **Pub/Sub streaming pull** subscription, so **no public webhook/HTTPS endpoint is needed** — the same long-running process that runs the Telegram bot also receives mail events.
- Each event carries a `historyId`; call `history.list` from the last stored id to fetch exactly what changed, then hand new messages to the parser.
- `watch()` expires ~7 days out, so **renew it daily** (a small in-service scheduled task). Losing the renewal silently stops all triggers, so alert if a renewal fails.

**One-time GCP setup:** a project (you likely already have one from the OAuth app), a Pub/Sub topic, a pull subscription, and a publish grant to `gmail-api-push@system.gserviceaccount.com` on the topic.

**Downtime resilience:** if the service is down, nothing is lost — Pub/Sub retains undelivered events (default 7-day retention) and redelivers on reconnect, and Gmail history can be replayed from the stored `historyId`. On startup the service reconciles from the last checkpoint before going live.

**Break-glass fallback:** the Watcher is written trigger-agnostic, so a `users.messages.list?q=from:rover@e.rover.com is:unread` poll is a one-flag fallback if push ever misbehaves. It is not the runtime path. (It's also a handy way to replay historical mail against the parser during development.)

**Getting Rover mail into the dedicated account:** simplest is a Gmail filter on your primary account that auto-forwards messages from `rover@e.rover.com` to the dedicated account. (Alternative: point Rover's notification email at the dedicated address if Rover lets you set that independently of your login.) Either way the watcher only ever authenticates against the dedicated account — your primary/report inbox is never exposed to this automation.

### 5.2 Parser + Deduper

From each notification email, extract:
- `owner_name`, `pet_name` — from the subject (`New message from {owner} about {pet}'s stay`).
- `stay_dates` — from the body line (`…a stay from MM/DD/YYYY to MM/DD/YYYY.`).
- `message_text` — the body between `{owner} says:` and the `Reply now` button.
- `thread_key` — the Gmail thread id (Rover groups a client's messages for one stay into a single Gmail thread, so this stays stable across the conversation).

Dedupe against the State Store on Gmail message id. **Debounce**: if several messages land within a short window (the sample shows two ~24 min apart), coalesce so you get one draft that accounts for the latest text rather than two.

### 5.3 State Store — SQLite

Single host, stdlib `sqlite3`, no ORM. Tracks each thread's `stage`, `status`, latest client message, latest draft, and flags. Source of truth for "what stage are we in / did we already draft this." See §6.

### 5.4 Stage Resolver + Context Builder

Looks up the thread's current `stage`, then assembles the LLM context:
- The **response playbook** (Appendix A) — templates + rules.
- An **optional `faq.md`** for answering ad-hoc client questions at S3. **Deferred** — not available at launch. The Context Builder loads it only if the file exists, so it drops in later with zero code changes. Until it exists, any question outside the playbook is flagged to you rather than drafted.
- The **client-side thread history** (reconstructed from the Gmail thread's prior notification emails), so the model sees what the client has said so far.

No availability. No pricing. Those are intentionally absent.

### 5.5 LLM Drafter — see §7.

### 5.6 Telegram Bot — see §8.

### 5.7 Supervisor

systemd user service, `Restart=on-failure`, linger already enabled (`loginctl enable-linger malikhack007`). Daily heartbeat + exception alerts pushed to the same Telegram chat so silent failures surface.

---

## 6. Data Model (SQLite)

```
threads
  thread_key        TEXT PK        -- Gmail thread id
  owner_name        TEXT
  pet_name          TEXT
  stay_dates        TEXT
  stage             TEXT           -- S0_INITIAL | S1_CONSENT | S2_ANSWERS | S3_POST_SCREEN
  status            TEXT           -- active | converted | not_suitable
  last_msg_text     TEXT
  last_draft_text   TEXT
  flags             TEXT
  created_at        TIMESTAMP
  updated_at        TIMESTAMP

messages                            -- optional audit log, one row per client message
  id                INTEGER PK
  thread_key        TEXT FK
  gmail_msg_id      TEXT UNIQUE     -- dedupe key
  direction         TEXT           -- inbound
  text              TEXT
  received_at       TIMESTAMP
```

**Stage advances** when you tap **Mark sent**. **Status** goes terminal (`converted` / `not_suitable`) via the corresponding buttons, after which the bot stops drafting for that thread.

---

## 7. LLM Design

Few-shot prompting (confirmed — no fine-tuning). "Pretraining" here means a curated prompt built from your playbook, not a trained model. Edits to your voice or rules are one text change away.

- **System prompt** encodes: your persona and tone, the stage machine, and hard rules —
  - *Availability is always assumed; never say you need to check dates.*
  - *Never quote or discuss pricing.*
  - *Reproduce the fixed templates (questionnaire, "About My Services" policy, park meet-and-greet) near-verbatim; only personalize the bracketed fields and the answer-dependent parts.*
  - *Only use information in the playbook / FAQ. If a client asks something not covered, do not invent policy — set a flag so it routes to Malik.*
  - *You are drafting, not booking. Final suitability is Malik's call via the screening flow.*
- **Few-shot examples**: the message pairs in Appendix A, bucketed by stage. Grows over time (you noted the pairs may expand — treat Appendix A as a living `playbook.md` in the repo).
- **Structured output**: return JSON — `{stage, draft_text, flags[], off_playbook: bool}` — parsed defensively (strip code fences, try/except).
- **Model**: one Claude Sonnet 5 call per message is plenty for this volume; a cheap Haiku 4.5 stage-classifier in front of a Sonnet drafter is an optional cost optimization, not needed for v1. Pin whatever model is current at build time.

---

## 8. Telegram UX

One message per new client message:

```
🐾 Vatsal · "Gypsy" · stay 08/26–08/28
Stage: S1 → sending questionnaire

Client said:
> sure!

Suggested reply:
[monospace / tap-to-copy block containing the 6-question
 questionnaire + the "About My Services" policy blurb]
```

Inline buttons:
- ✅ **Mark sent** — advance the thread to the next stage
- 🔁 **Regenerate** — new draft, same stage
- ✏️ **Warmer / Shorter** — tone nudge, re-draft
- 🎉 **Converted** — terminal; stop drafting this thread
- 🚫 **Not suitable** — terminal; stop drafting this thread

If the model set `off_playbook`, the message leads with **⚠️ Needs your attention** and shows the raw client text with *no* draft (e.g. mid-stay logistics like "I'm tracking for 3:30").

Tap-to-copy is the copy mechanism — Telegram copies a monospace/code block on tap (bots can't reach the clipboard). You copy, paste into Rover, tap **Mark sent**. Button callbacks are why this runs as a persistent service, not a timer.

---

## 9. Tech Stack (explicit)

| Concern | Choice | Notes |
|---|---|---|
| Language | **Python 3.10** | matches the repo / `.venv` |
| Inbox read | **Gmail API** (`google-api-python-client`, `google-auth`) | `gmail.readonly` on a **dedicated account**, separate OAuth creds from the report bot |
| Instant trigger | **Gmail `watch` + Cloud Pub/Sub (streaming pull)** | primary from day one; seconds-latency, no public webhook; renew `watch()` daily. Poll is a break-glass fallback only |
| LLM | **Anthropic API** via `anthropic` SDK | Claude Sonnet 5 drafter; optional Haiku 4.5 classifier |
| Messaging | **`python-telegram-bot`** | long-polling + inline keyboards + callbacks |
| State | **SQLite** (`sqlite3` stdlib) | single host |
| Config/secrets | **`.env` + `python-dotenv`** | `load_dotenv()` at top of every entrypoint (your standing rule) |
| Process mgmt | **systemd user service** + linger | long-running, 24/7; `Restart=on-failure` |
| Logging | Python `logging` → file + Telegram alerts | daily heartbeat + exceptions |

**Secrets (`.env`):** `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, dedicated-account Gmail OAuth token path, `PUBSUB_SUBSCRIPTION` (if using push). No Playwright, no Rover credentials anywhere in this system.

---

## 10. Deployment & Ops

- **Unit:** `~/.config/systemd/user/rover-autoresponder.service`, `Restart=on-failure`, `WantedBy=default.target`.
- **Linger:** already enabled — survives logout. Service runs 24/7 (no quiet hours); `watch()` is renewed daily by an in-service task.
- **OAuth:** dedicated-account app published to "In production" (no 7-day refresh-token expiry, matching your report-bot setup). Keep these creds separate from the primary account's.
- **Secrets:** `.env`, never committed. (Per your git-hygiene pass: if a secret ever lands in a commit, rotate it.)
- **Design-doc-before-code:** this doc is that step.

---

## 11. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Rover ToS / automation stance | Zero Rover automation — reads your own email, sends nothing to Rover. Lowest-footprint approach; you've accepted this residual risk. |
| Bot drafts to a post-booking / off-playbook message | `off_playbook` flag → no draft, just a heads-up; `converted` threads stop drafting entirely. The Vatsal sample is exactly this case. |
| Model invents policy/price not in playbook | System prompt forbids it; uncovered questions are flagged. Providing the FAQ doc shrinks this surface. |
| Stage drift (you edit/send off-script) | Model also reads client-side thread history to self-correct; stage is a hint, not a hard gate. |
| Gmail OAuth / `watch` expiry (silent trigger loss) | Daily `watch()` renewal; alert on renewal or auth failure so a dead trigger doesn't go unnoticed. |
| Service downtime | Pub/Sub retains events (7-day default) and redelivers on reconnect; Gmail history replays from the stored `historyId`; startup reconciles from the last checkpoint before going live. |
| Duplicate drafts / rapid messages | Dedupe on Gmail message id; debounce window coalesces bursts. |

---

## 12. Decisions Log

All prior open questions are resolved. For the record:

- **Trigger:** Gmail push (watch + Pub/Sub streaming pull) from the start; fast-poll is a break-glass fallback only. (§5.1)
- **FAQ doc:** deferred — not available yet, will be added later as an optional `faq.md`. Until then, uncovered S3 questions are flagged, not drafted. (§5.4)
- **Hours:** run 24/7, no overnight suppression.
- **Read source:** notification emails carry the full message text — email-only, no Rover scraping. (§4)
- **Account:** dedicated Gmail account for Rover messages, isolated from the primary/report account. (§5.1)
- **Availability:** always assumed — never referenced in drafts. **Pricing:** never referenced (clients see it at request time). (§2)
- **Scope:** full multi-turn screening flow; the bot keeps drafting follow-ups until you mark the thread `converted` or `not_suitable`. (§4)
- **LLM:** few-shot prompting, no fine-tuning. (§7)

**Deferred inputs (not blocking a build):** the `faq.md`, and additional few-shot pairs as the playbook grows.

---

## 13. Phasing

- **Phase 1 —** Dedicated Gmail account + forwarding filter; GCP Pub/Sub topic/subscription + `watch()` push + daily renewal; parser + SQLite dedup; log parsed messages only (no LLM, no Telegram). Prove the read side end-to-end on the real trigger. *(During dev, a manual `messages.list` pull can replay historical mail against the parser before push is fully wired.)*
- **Phase 2 —** Stage machine + LLM drafter (playbook system prompt + few-shot); print drafts + stage to log.
- **Phase 3 —** Telegram delivery (send-only, tap-to-copy).
- **Phase 4 —** Telegram buttons + stage/status transitions; systemd user service (24/7) + linger; downtime reconciliation on startup.
- **Phase 5 —** Polish: alerting/heartbeat, debounce tuning, and wire in `faq.md` when you supply it.

---

## Appendix A — Response Playbook v1 (source of truth for the prompt)

*Living document; expand as the project develops.*

**S0 — Initial inquiry.** Client: *"Are you available to sit [dog] from _ to _?"*
> Hey [owner_name], [dog_name] looks adorable! Definitely happy to potentially watch your pup for you. Do you mind answering a few quick questions to make sure we're a good fit?

**S1 — Client consents** (typically "sure!" / "yes!"). Send the questionnaire:
> Awesome!
> 1. Where are you in your sitter search? Are you seriously considering booking with me, or still browsing a few other sitters?
> 2. Does your dog experience separation anxiety? If so, do they need someone with them at all times, or are they okay being alone briefly?
> 3. How often do you typically walk your dog each day? Do they have a set schedule or any special walking needs?
> 4. Has your dog ever shown aggression toward other dogs? This helps me keep a safe, comfortable environment for both my pup and yours.
> 5. Does your dog experience submissive urination? (They pee every time someone new touches them, out of excitement/submissiveness.) If so, how bad is it?
> 6. Do you have any specific expectations with photo updates? Are you comfortable with a once-a-day update, with additional updates upon request?

…and set the no-entry policy:
> 🐾 About My Services (please read — important info):
> Your dog's safety and comfort are my top priorities. I provide a calm, home-like environment with plenty of love, walks, and supervision — just like they're part of the family. Throughout the stay I'll send at least one photo update per day, and more if you just shoot over a text!
> One policy I implement for our safety and my privacy: I do not allow clients to enter my home. (Rover doesn't do background checks on dog owners.) If you'd like to see the space, I'm more than happy to do a live video call. If physically seeing my place is a deal breaker, I totally understand — just let me know and I'll archive this request 😁

**S2 — Client answers.** Respond (personalize to their answers):
> Thanks for the quick & comprehensive answers! I'd be happy to take care of [dog_name] based on your description! Any questions for me?

**S3 — Meet-and-greet / video call.**
- Meet-and-greet:
  > Happy to do a meet and greet! What does your availability look like in the next 7 days? We can meet at the Brownie Neighborhood Park! https://share.google/VutyY9GThICbr6BTy
- Insists on the house:
  > Yes, we can do a meet & greet outside my house. I'll take your pup into my house alone for an initial intro, then bring both out to my driveway to show you how they get along, assuming the introduction inside went smoothly!
- Live video call:
  > Ask what time today they're available for the call.