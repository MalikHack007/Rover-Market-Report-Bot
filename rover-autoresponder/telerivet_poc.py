#!/usr/bin/env python3
"""Proof of concept — send a full-size MMS (image) via Telerivet's REST API.

Why Telerivet (after httpSMS): httpSMS caps MMS attachments at a few KB (per their
support) — useless for real photos. Telerivet is a general-purpose Android SMS/MMS gateway
that sends MMS with real media from your phone's own SIM/number. This PoC's WHOLE POINT is
to confirm a few-MB image actually goes through, so it sends an image by default.

How it works (cloud mode): Telerivet's cloud tells the registered Android app (the Pixel
gateway) to send; the phone downloads each media URL and sends the MMS from your number. So
the media must be a PUBLIC URL Telerivet/the phone can fetch, and the phone needs internet.
A 200 here means QUEUED, not delivered — watch the handset and the Telerivet dashboard
(which shows the failure reason, e.g. a size error, if it doesn't go).

Prerequisites (one-time):
    1. Install the "Telerivet Gateway" app on the Pixel, sign in, add the phone to a
       Telerivet project. MMS must be enabled on the phone/carrier.
    2. From the project's API page, copy: API key, Project ID (PJ...), and Phone ID (PN...).

Env (put in .env; API key is a SECRET, keep it out of git):
    TELERIVET_API_KEY     API key (used as the HTTP Basic Auth username)
    TELERIVET_PROJECT_ID  the project id, e.g. PJxxxxxxxxxxxxxxxx
    TELERIVET_PHONE_ID    the gateway phone's id (PN...) — which phone/number sends
                          (optional; omit to use the project's default phone)

Usage:
    python telerivet_poc.py                                  # ~1-2MB test image -> +13475705058
    python telerivet_poc.py --image https://.../dog.jpg      # one image by public URL
    python telerivet_poc.py --text-only --text "hi"          # plain SMS, no media

    # MULTI-IMAGE test (the open question): several real full-size photos in ONE MMS.
    # --photo uploads each local file to R2 (oriented, original size) then attaches it,
    # exactly like the real pipeline. Repeat --photo (or --image) for each image:
    python telerivet_poc.py --photo dog1.jpg --photo dog2.jpg --photo dog3.jpg
"""
import argparse
import os
import re
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv
import requests

load_dotenv()  # standing project rule: load .env before reading os.environ

API_URL = "https://api.telerivet.com/v1/projects/{project_id}/messages/send"
DEFAULT_TO = "3475705058"
# A ~1-2 MB photographic JPEG, so we actually exercise the size limit that killed httpSMS
# (its few-KB cap). Swap in a real dog photo with --image. (This URL 302-redirects to a
# CDN image; if Telerivet won't follow it, pass a direct, non-redirecting URL instead.)
DEFAULT_IMAGE = "https://picsum.photos/2400/1600"


def e164(raw):
    """Normalize a US number to E.164 (+1XXXXXXXXXX). Returns None if unparseable."""
    if raw and raw.strip().startswith("+"):
        return "+" + re.sub(r"\D", "", raw)
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def _media_item(url):
    """Build one Telerivet `media` entry ({url, type, filename}) from a URL."""
    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
    return {"url": url, "type": mime, "filename": os.path.basename(path) or "photo.jpg"}


def main():
    ap = argparse.ArgumentParser(description="Telerivet MMS proof of concept")
    ap.add_argument("--to", default=DEFAULT_TO, help=f"recipient number (default {DEFAULT_TO})")
    ap.add_argument("--image", action="append", metavar="URL",
                    help="public image URL to attach; REPEATABLE — several = one multi-image MMS")
    ap.add_argument("--photo", action="append", metavar="FILE",
                    help="local photo to upload to R2 first, then attach; REPEATABLE. Oriented + "
                         "kept at original size, exactly like the real pipeline (needs R2 env).")
    ap.add_argument("--text-only", action="store_true", help="send a plain SMS, no media")
    ap.add_argument("--text", default="Test photo from the Rover bot 🐾 (Telerivet PoC).",
                    help="message body / caption")
    args = ap.parse_args()

    api_key = os.environ.get("TELERIVET_API_KEY")
    project_id = os.environ.get("TELERIVET_PROJECT_ID")
    phone_id = os.environ.get("TELERIVET_PHONE_ID")   # optional
    missing = [n for n, v in (("TELERIVET_API_KEY", api_key),
                              ("TELERIVET_PROJECT_ID", project_id)) if not v]
    if missing:
        sys.exit("Missing env var(s): " + ", ".join(missing) +
                 " — set them in .env (see this file's docstring).")

    to = e164(args.to)
    if not to:
        sys.exit(f"--to {args.to!r} isn't a US number I can parse — use 10 digits or +1...")

    # Build the media list: public URLs as-is, plus local --photo files uploaded to R2.
    if args.text_only:
        media_urls = []
    else:
        media_urls = [u for u in (args.image or []) if u]
        if args.photo:
            from r2_upload import upload_and_presign  # orient + original size + presign
            for p in args.photo:
                print(f"  uploading {p} to R2 …")
                try:
                    media_urls.append(upload_and_presign(p))
                except Exception as e:
                    sys.exit(f"R2 upload failed for {p}: {type(e).__name__}: {e}")
        if not media_urls:                              # bare run: one default test image
            media_urls = [DEFAULT_IMAGE]

    payload = {"content": args.text, "to_number": to}
    if phone_id:
        payload["phone_id"] = phone_id                  # which registered phone/number sends
    if media_urls:
        payload["media"] = [_media_item(u) for u in media_urls]  # non-empty -> MMS

    n = len(media_urls)
    kind = f"MMS ({n} image{'s' if n != 1 else ''})" if n else "SMS"
    url = API_URL.format(project_id=project_id)
    print(f"POST {url}")
    print(f"  to={to}  kind={kind}  phone_id={phone_id or '(project default)'}")
    for u in media_urls:
        print(f"  media={u[:90]}{'…' if len(u) > 90 else ''}")
    try:
        r = requests.post(url, auth=(api_key, ""), json=payload, timeout=(5, 30))
    except requests.RequestException as e:
        sys.exit(f"Could not reach Telerivet: {type(e).__name__}: {e}")

    print("HTTP", r.status_code)
    print(r.text[:1500])
    if r.status_code == 200:
        print(f"\n✅ Accepted by Telerivet (QUEUED, not delivered). It relays to the phone, "
              f"which downloads the media and sends the {kind} from your number.")
        if n > 1:
            print("   MULTI-IMAGE TEST — on the recipient handset, check whether ALL "
                  f"{n} images arrived in ONE message, split into several, or failed. The "
                  "Telerivet dashboard message log shows the authoritative status/failure "
                  "reason (e.g. a size/count limit).")
        else:
            print("   Confirm on the recipient handset AND the Telerivet dashboard log — if "
                  "the photo is too big, the dashboard shows the failure reason.")
    else:
        print("\n❌ Telerivet rejected the request. Check TELERIVET_API_KEY / PROJECT_ID / "
              "PHONE_ID, and that the phone is online with MMS enabled.")
        sys.exit(1)


if __name__ == "__main__":
    main()
