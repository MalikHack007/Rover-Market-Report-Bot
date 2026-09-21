# Handoff: Dog-Sitting Availability Calendar

## Overview
A single static HTML page that displays a dog-sitter's monthly availability on a
calendar. The page fetches an `availability.json` file and renders each day of the
month as Available, Away, or unset. Tapping a day reveals that day's status in plain
language. The page also shows the sitter's name, photo, and a short bio.

This is a read-only, public-facing page — no login, no booking transaction. Tapping a
day only surfaces its status.

## About the Design Files
The files in this bundle are **design references created in HTML** — a prototype that
shows the intended look and behavior, not production code to ship as-is. The task is to
**recreate this design in your target environment** (React/Vue/Svelte/plain static
HTML, whatever the project uses), following that project's established patterns. If
there is no existing environment, plain static HTML + a little vanilla JS is a perfectly
good fit here — this is a static page that reads one JSON file. There is no backend
requirement.

## Fidelity
**High-fidelity.** Colors, typography, spacing, and interaction states below are final.
Recreate the UI to match. All colors are given in `oklch()`; hex fallbacks are listed in
Design Tokens.

## Data Contract — `availability.json`
The page reads a single JSON file with this exact shape:

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

- `days` — the source of truth. An object keyed by **full ISO date** (`"YYYY-MM-DD"`) →
  **boolean**: `true` = Available, `false` = Away.
- `default` — the status to use for any date **not present** in `days` (and within the
  horizon). Value `"unknown"` renders as the neutral/unset state. If a future feed sends
  `true`/`false` here, treat unlisted days accordingly.
- `horizon_days` — how many days ahead of `generated_at` the feed is meaningful. Dates
  beyond `generated_at + horizon_days` (and any date before `generated_at`) should be
  treated as out-of-range: render neutral and, ideally, non-emphasized. This is advisory;
  the minimum viable page can ignore it and just fall back to `default`.
- `generated_at` — ISO timestamp the feed was produced. Optional to surface (e.g. a
  small "updated 2 hours ago" note); not required.
- `timezone` — IANA tz the sitter operates in. Use it if you localize `generated_at`;
  the calendar grid itself is plain calendar dates and needs no tz math.

**Lookup per cell:** build each day's ISO string (`YYYY-MM-DD`, zero-padded) and read
`days[iso]`. `true` → available, `false` → away, missing → `default` (→ unset when
`"unknown"`).

Fetch on load, e.g. `fetch("availability.json").then(r => r.json())`. Handle the pending
state gracefully (the grid can render neutral until data arrives). The prototype embeds
equivalent data as a fallback for offline preview — replace that with the fetch.

## Screens / Views
Single view: **Availability Calendar** (one card, centered on the page).

### Layout
- Page: full-viewport, centered both axes, background `oklch(0.96 0.004 260)` (near-white
  cool grey), padding `48px 20px`.
- Card: `max-width: 560px`, full width below that, background `oklch(0.99 0.003 250)`,
  `border-radius: 6px`, `1px` border `oklch(0.92 0.004 260)`, shadow
  `0 20px 50px -24px oklch(0.4 0.02 260 / 0.5)`. Two stacked regions:

**1. Header** — padding `44px 40px 28px`, bottom border `1px oklch(0.93 0.004 260)`.
Horizontal flex, `gap: 16px`, vertically centered:
- Avatar: `64×64px` circle. In the prototype it is a diagonal-stripe placeholder
  (`repeating-linear-gradient`) reading "your photo". Replace with the sitter's real
  photo (`object-fit: cover`, circular).
- Text block:
  - Name: `Bricolage Grotesque` 700, `25px`, line-height `1.05`, color
    `oklch(0.24 0.01 260)`, `letter-spacing: -0.01em`. Copy: **"Marlowe's Dog Sitting"**.
  - Bio: `Work Sans` 400, `14px`, line-height `1.45`, color `oklch(0.5 0.01 260)`,
    `max-width: 360px`, `margin-top: 5px`. Copy: **"Cozy, cage-free care for good boys &
    girls. Weekend & holiday spots fill fast."**

**2. Calendar body** — padding `26px 40px 40px`.
- Top row (flex, space-between, `margin-bottom: 20px`):
  - Left: month navigator — a `‹` button, the month label, a `›` button.
    - Nav buttons: `32×32px` circle, `18px` glyph, color `oklch(0.45 0.01 260)`,
      cursor pointer, hover background `oklch(0.95 0.004 260)`.
    - Month label: `Bricolage Grotesque` 700, `20px`, color `oklch(0.24 0.01 260)`,
      `letter-spacing: -0.01em`, `min-width: 150px`, centered. Format: "September 2026".
  - Right: legend — two items, `gap: 18px`, `Work Sans` 500 `12px` color
    `oklch(0.45 0.01 260)`. Each: an `8px` dot + label. Available dot
    `oklch(0.62 0.14 150)` (green); Away dot `oklch(0.82 0.01 260)` (grey).
- Weekday header + day grid: one `display: grid`, `grid-template-columns: repeat(7, 1fr)`,
  `gap: 2px`. Week starts **Sunday**.
  - Weekday labels (Sun–Sat, 3-letter): `Work Sans` 600 `10px`, uppercase,
    `letter-spacing: .08em`, color `oklch(0.6 0.01 260)`, centered, `padding-bottom: 10px`.
  - Day cells: `54×54px`, `margin: 2px auto`, circle (`border-radius: 50%`), centered
    content, `Work Sans` 500 `15px`, `1.5px solid transparent` border by default.
    Leading blank cells before day 1 are `visibility: hidden`.

### Day cell states
| State | Text color | Border | Extra |
|---|---|---|---|
| available | `oklch(0.28 0.01 260)` | `1.5px solid oklch(0.62 0.14 150)` (green ring) | cursor pointer |
| away | `oklch(0.75 0.01 260)` | transparent | `text-decoration: line-through`, cursor pointer |
| unset (not in JSON) | `oklch(0.55 0.01 260)` | transparent | not emphasized; still tappable |
| selected (any) | `oklch(0.99 0.003 250)` on `oklch(0.24 0.01 260)` fill | `1.5px solid oklch(0.24 0.01 260)` | overrides the above |

- **Status line** below the grid: `margin-top: 24px`, `min-height: 22px`, `Work Sans` 500
  `14px`, color `oklch(0.4 0.01 260)`.
  - Default (nothing selected): "Tap a day to see its status".
  - available: `"{Weekday, Month D} — Available for booking"`.
  - away: `"{Weekday, Month D} — Away, not taking dogs"`.
  - unset: `"{Weekday, Month D} — No availability set"`.
  - Date format example: "Wednesday, September 2".

## Interactions & Behavior
- **Tap a day** → set it as selected; fill it dark, update the status line. Only one day
  selected at a time.
- **Prev / next month** (`‹` / `›`) → change the visible month, clear the current
  selection. Rolls the year over at Dec↔Jan. Look up the new month's key in the JSON;
  unknown months simply render all-neutral.
- **Hover** on nav buttons → light grey background. Day cells have no distinct hover
  state in the prototype (optional to add a subtle one matching the codebase).
- No animations beyond default; transitions are optional and should be subtle if added.
- Responsive: the card is fluid up to `560px`. On narrow screens it fills width minus the
  `20px` page padding. The 7-column grid stays 7 columns; cells may shrink — consider
  making cell size `min(54px, …)` or using `aspect-ratio: 1` with `1fr` columns if you
  want them to scale down cleanly on very small phones.

## State Management
- `year` (number), `month` (0-indexed number) — the visible month. Defaults to the
  current month in production (prototype pins September 2026).
- `feed` — the parsed JSON object (null until fetch resolves); read `feed.days` and
  `feed.default` per cell.
- `sel` — the selected day `{ day, status }` or null; cleared on month change.
- Data fetch: one GET of `availability.json` on mount.

## Design Tokens
Colors (oklch → approximate hex):
- Page bg `oklch(0.96 0.004 260)` ≈ `#f2f3f4`
- Card bg `oklch(0.99 0.003 250)` ≈ `#fbfbfc`
- Card border `oklch(0.92 0.004 260)` ≈ `#e7e8ea`
- Ink / headings `oklch(0.24 0.01 260)` ≈ `#2b2d31`
- Body text `oklch(0.5 0.01 260)` ≈ `#6f7176`
- Muted / status line `oklch(0.4 0.01 260)` ≈ `#565a5f`
- Weekday / legend `oklch(0.6 0.01 260)` ≈ `#8a8d92`
- Available green (ring/dot) `oklch(0.62 0.14 150)` ≈ `#1f9e6b`
- Away grey (dot) `oklch(0.82 0.01 260)` ≈ `#cbccce`
- Available day text `oklch(0.28 0.01 260)`; Away day text `oklch(0.75 0.01 260)`; unset day text `oklch(0.55 0.01 260)`

Typography:
- Display / headings & month label: **Bricolage Grotesque**, 700
- UI & body: **Work Sans**, 400 / 500 / 600
- Load via Google Fonts (link in the prototype `<head>`).

Radius: card `6px`; day cells & avatar & nav buttons `50%`.
Shadow: card `0 20px 50px -24px oklch(0.4 0.02 260 / 0.5)`.
Spacing: card padding `44px 40px 28px` (header) / `26px 40px 40px` (body); grid gap `2px`;
legend gap `18px`.

## Assets
- **Sitter photo**: the only real image. Prototype uses a striped placeholder; supply a
  square photo, rendered as a `64px` circle with `object-fit: cover`.
- No icon set required — the `‹`/`›` are text glyphs and the legend dots are CSS.
- Fonts are from Google Fonts (Bricolage Grotesque, Work Sans).

## Files
- `Dog Sitting Calendar.dc.html` — the design prototype (this is an Omelette "Design
  Component" HTML file; open it in a browser to see the live design). Read it as a visual
  and behavioral reference. The calendar-building logic (grid generation, month shifting,
  status phrasing) at the bottom of the file is directly portable.
- `availability.json` — sample data in the exact contract the page expects (real shape:
  `days` keyed by ISO date → boolean, plus feed metadata). Use it as the seed / test
  fixture.
