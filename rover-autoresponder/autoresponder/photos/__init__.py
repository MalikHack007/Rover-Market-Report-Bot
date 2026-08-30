"""Photo-update assistant (Addendum C) — an isolated subpackage.

Malik taps a dog on the roster, sends its photo(s), a caption is picked from a pool, and on
one Send-all tap the bot MMSes each dog's photos to its owner via Telerivet (from Malik's own
number), with images hosted on Cloudflare R2. Everything feature-specific lives here so it
doesn't clutter the top-level `autoresponder/` package.

Design: ../../rover_photo_updates_design_addendum_C.md
Modules:
  config    — feature settings (Telerivet / R2 / captions / budgets)
  hosting   — R2 upload + presign (EXIF-oriented, original size)
  telerivet — Telerivet send (from Malik's number) + batched delivery-status query
  captions  — pre-written caption pool (no LLM): pick + {pet} substitution
  store     — photo_updates / photo_update_media schema + CRUD (shared conn + lock)
  pipeline  — [next] Telegram tap-to-assign intake, roster, review cards
  approve   — [next] batch Send-all, edit, API-budget guardrail
  poller    — [next] batched delivery-status poller
"""
