# Rover Daily Rank & Pricing Bot — Design Document

**Status:** Draft for review
**Owner:** Malik
**Last updated:** 2026-06-21

---

## 1. Purpose

A scheduled bot that, once per day, checks a fixed set of Austin zip codes on Rover and reports, for an overnight-boarding search covering tonight (today → next day):

1. Where the operator's own listing ranks on page 1 of each zip (or that it is absent).
2. The median nightly boarding price in each zip.
3. The overall median nightly price pooled across all zips.

The result is delivered as a daily email report. The bot exists to give the operator a low-effort, repeatable read on their competitive position and the local price market without manually running five searches every morning.

---

## 2. Goals and Non-Goals

**Goals**

- Fully unattended daily operation.
- Accurate per-zip rank and median from page 1 only.
- A single, readable email report plus a persisted history log.
- A light, polite footprint on Rover (a handful of page-1 fetches per day).

**Non-Goals**

- No paging beyond page 1.
- No circumventing Cloudflare or any anti-bot control (see §9). If page 1 stops loading cleanly, the bot reports the failure rather than escalating.
- No posting, messaging, booking, or any write action on Rover. Read-only.
- No multi-night or multi-service analysis in v1 (overnight boarding, one night, only).

---

## 3. Inputs

### 3.1 Target zips

| Zip | Neighborhood | Note |
|-----|--------------|------|
| 78753 | Windsor Hills | Operator's home area |
| 78723 | Windsor Park | |
| 78701 | Downtown | |
| 78757 | Crestview | |
| 78751 | Hyde Park | |

### 3.2 Search parameters (fixed)

- **Service type:** overnight boarding
- **Date window:** `start_date = today`, `end_date = today + 1 day` (one night), recomputed each run.
- **Page:** 1 only.
- **Pet config / filters:** held constant, inherited from the operator's captured search URLs.

### 3.3 Operator identity

- `MY_SITTER_NAME`: the operator's display name exactly as Rover renders it (e.g. `Malik Z.`). Used to locate the operator's own card within page-1 results.

---

## 4. Outputs

For each zip:

- **Rank:** the 1-based position of the operator's card in page-1 display order, or the literal string **`NOT ON FIRST PAGE`** when the operator's name is not found.
- **Median price:** median of all per-night rates on page 1 of that zip.
- **Sitter count:** number of rate-bearing cards parsed (sample size).

Across all zips:

- **Overall median:** median of every per-night rate **pooled** across all five zips (one market-wide figure, weighted naturally by how many sitters each zip contributes).

Delivery:

- **Email:** an HTML report (with a plain-text fallback) sent to the operator's own inbox.
- **History log:** one row per zip per day appended to a CSV, enabling later trend analysis.

---

## 5. Architecture

A single Python script, triggered by cron, that runs a linear pipeline and exits. Because the report is one-way (bot → operator, no interaction or approval), no long-running process or server is required.

```
cron (daily @ set time)
      │
      ▼
┌───────────────────────────────────────────────┐
│ rover_report.py                                │
│                                                │
│  1. Compute tonight's date window              │
│  2. Launch headless Chromium (one context)     │
│  3. For each zip:                              │
│        build URL → fetch page 1 → parse cards  │
│        → per-zip rank + median                 │
│        (spaced delay between zips)             │
│  4. Pool all rates → overall median            │
│  5. Render email (HTML + text)                 │
│  6. Append run to CSV history                  │
│  7. Send email via Gmail API                   │
└───────────────────────────────────────────────┘
```

### 5.1 URL generation

Each zip's full search URL is captured **once** from the browser and frozen as a per-zip template. At runtime the bot rewrites only `start_date`, `end_date`, and `page` via targeted substitution; every other parameter (including `centerlat`/`centerlng`, which Rover derives per zip) is left untouched. This avoids geocoding entirely and is reliable precisely because the zip set is fixed.

### 5.2 Fetching

Headless Chromium loads each page-1 URL, waiting on rendered price content (`domcontentloaded` plus an explicit wait for a "night" rate to appear) rather than network idle, which never settles on Rover. A full-page screenshot is saved per zip for verification. Fetches are spaced with a randomized delay so the daily batch does not resemble a burst.

### 5.3 Parsing

Prices are extracted from rendered card text, accepting a value only when it sits directly adjacent to "night" (i.e. a genuine `$X per night` rate), and excluding `<script>`/`<style>` content so localization and data blobs cannot leak false matches. Each card's text is captured by climbing from the rate node up to a card-sized block so it also contains the sitter's name.

### 5.4 Rank determination

Cards are collected in document (display) order. Rank is the 1-based index of the first card whose text contains `MY_SITTER_NAME`. No match → `NOT ON FIRST PAGE`. Reported rank reflects Rover's actual displayed order, which includes sponsored/premier placements.

### 5.5 Aggregation

Per-zip median = median of that zip's rates. Overall median = median of all zips' rates pooled into one list.

### 5.6 Reporting and logging

The report renders as an HTML table (zip / rank / median / sitter count) with the pooled aggregate beneath it, plus a plain-text alternative. Every run also appends rows to `report_log.csv` (`date, zip, rank, median, n_sitters`, plus an `AGGREGATE` row), building history for future trend work.

### 5.7 Delivery

Email is sent through the Gmail API using OAuth with the `gmail.send` scope only. First authorization is interactive (one-time, by hand); subsequent cron runs are unattended via the cached, auto-refreshing token.

---

## 6. Tech Stack

| Concern | Choice | Rationale |
|---------|--------|-----------|
| Language | Python 3.10 | Operator's existing environment (venv) |
| Browser automation | Playwright (Chromium, headless) | Rover has no API and is JS-rendered; Playwright renders the SPA |
| Price/card parsing | Regex over live DOM text (Playwright `eval_on_selector_all`) | Card CSS classes are hashed; text anchors ("per night") are more stable |
| Email delivery | Gmail API via `google-api-python-client` + `google-auth-oauthlib` | Gmail already connected; OAuth avoids storing a password; minimal `gmail.send` scope |
| Scheduling | cron | Native on Xubuntu; fits a fire-and-exit job better than an always-on scheduler |
| History storage | CSV file | Trivial, inspectable, sufficient for daily rows; no DB needed |
| Config | In-module constants + Google OAuth files (`credentials.json`, `token_send.json`) | Small, single-operator tool |
| OS / runtime | Xubuntu Linux, Python venv | Operator's machine |

**External dependencies:** `playwright`, `google-api-python-client`, `google-auth-httplib2`, `google-auth-oauthlib`, plus the Chromium build from `playwright install`.

---

## 7. Requirements

### 7.1 Functional

- **FR1** — Run unattended once per day at a configured time.
- **FR2** — For each of the five zips, fetch page 1 of an overnight-boarding search for today → tomorrow.
- **FR3** — Report the operator's page-1 rank per zip, or `NOT ON FIRST PAGE` when absent.
- **FR4** — Report the median nightly price per zip.
- **FR5** — Report the overall median nightly price pooled across all zips.
- **FR6** — Deliver all of the above as a single daily email.
- **FR7** — Append each run's results to a CSV history log.

### 7.2 Non-Functional

- **NFR1 — Low footprint:** a small number of page-1 fetches per day, randomized spacing between them; no anti-bot circumvention of any kind.
- **NFR2 — Unattended robustness:** absolute paths throughout (cron's minimal env), auto-refreshing OAuth token, failures written to a log rather than lost.
- **NFR3 — Fault isolation:** a single zip failing to load (empty page or a Cloudflare check) is recorded for that zip and does not abort the run; the rest still report.
- **NFR4 — Least privilege:** Gmail scope limited to `gmail.send`; no inbox read access.
- **NFR5 — Maintainability:** zips, identity, recipient, and timing are configuration, not code changes; adding a zip is pasting one captured URL.
- **NFR6 — Compliance awareness:** see §9; the bot stays read-only, low-volume, and personal-use, and does not defeat access controls.

---

## 8. Configuration

| Key | Meaning |
|-----|---------|
| `MY_SITTER_NAME` | Operator's Rover display name, for rank matching |
| `EMAIL_TO` | Report recipient (operator's own inbox) |
| `ZIP_URLS` | Map of zip → captured search URL (template) |
| `DELAY` | Min/max seconds between per-zip fetches |
| `HEADLESS` | Headless toggle (default on) |
| Google OAuth files | `credentials.json` (from Cloud Console), `token_send.json` (cached after first auth) |

---

## 9. Constraints, Risks & Mitigations

**No public Rover API.** Confirmed; the only path is rendering the site, hence Playwright.

**Cloudflare bot detection.** Empirically, single page-1 fetches load cleanly headless; rapid repeated navigation (e.g. paging deeper) triggers a challenge. *Mitigation:* page 1 only, five spaced fetches per day. *Boundary:* if Cloudflare begins challenging the daily batch, the response is wider spacing or fewer zips — **not** stealth tooling, CAPTCHA solvers, or proxies. The bot treats a challenge as a "stop," reports it for that zip, and moves on.

**Terms of Service.** Rover's ToS restricts automated access. The design keeps the footprint minimal, read-only, and personal-use, but the operator accepts this residual risk; the bot is explicitly scoped to never take write actions on the platform.

**DOM fragility.** Rover's card markup uses hashed/obfuscated class names that can change. *Mitigation:* parsing keys on stable text ("per night") rather than class names. The rank feature additionally relies on a card-boundary heuristic (climbing to a name-bearing block), which **must be verified once** against a real page (via the saved screenshots) and re-pinned if Rover's structure shifts.

**Rank interpretation.** Displayed order includes sponsored/premier placements, so rank reflects what a customer actually sees, not a pure merit ordering. The report notes this.

**Silent cron failure.** A common operational pitfall. *Mitigation:* absolute paths, redirect stdout/stderr to `cron.log`, and a one-time manual run to complete OAuth before scheduling.

---

## 10. Failure Modes

| Failure | Behavior |
|---------|----------|
| One zip page empty / Cloudflare-challenged | That zip recorded with no rank/median; run continues |
| All zips fail | Report still sent/logged showing the failures |
| Email send fails | Report text written to `cron.log` so the data isn't lost |
| Operator not on page 1 | `NOT ON FIRST PAGE` for that zip; median still computed |
| OAuth token expired/revoked | Auto-refresh; if refresh fails, error logged, needs one manual re-auth |

---

## 11. Future Extensions (out of scope for v1)

- Day-over-day deltas in the email (rank moved +2, median +$3), computed from the CSV history.
- Multi-night or weekend-vs-weekday price curves.
- Trend charts attached to the email.
- Alerting only when something changes materially (rank drop past a threshold).

---

## 12. Open Decisions (to confirm before build)

1. **Run time of day** for the cron schedule is 8 AM CDT.
2. **First-auth machine:** authorize on the same Xubuntu box that runs cron (so `token_send.json` lives there). Yes
3. **Aggregate definition:** confirmed as pooled across all sitters (not an average of per-zip medians). Yes, pooled across all sitters.
4. **Screenshots retention:** keep per-zip screenshots for verification, or only on failure to reduce clutter? - Only on failture to reduce clutter.