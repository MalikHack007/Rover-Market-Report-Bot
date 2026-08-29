#!/usr/bin/env python3
"""Upload an image to Cloudflare R2 and return a short-lived presigned URL.

The hosting building block for photo updates: Telerivet's gateway needs a PUBLIC url to
fetch the media, so we upload the photo to R2 and hand back a presigned GET url that
expires (default 1h). Pair it with telerivet_poc.py:

    URL=$(python r2_upload.py path/to/dog.jpg)
    python telerivet_poc.py --image "$URL"

Or import it:

    from r2_upload import upload_and_presign
    url = upload_and_presign("dog.jpg")            # -> https://...  (expires after TTL)

Privacy: the object key is randomized (unguessable) and the presigned url expires. Set a
bucket lifecycle rule to auto-delete objects after ~1 day so client photos don't linger.

R2 credentials come from an R2 API token (Cloudflare dashboard -> R2 -> Manage API Tokens).
Env (put in .env; the keys are SECRETS):
    R2_ACCOUNT_ID         Cloudflare account id (used to build the S3 endpoint), OR
    R2_ENDPOINT_URL       full endpoint, e.g. https://<account_id>.r2.cloudflarestorage.com
    R2_ACCESS_KEY_ID      R2 token access key id
    R2_SECRET_ACCESS_KEY  R2 token secret
    R2_BUCKET             bucket name
    R2_PRESIGN_TTL        presigned url lifetime in seconds (default 3600)
    R2_IMAGE_QUALITY      JPEG quality for the re-encode ONLY when a rotated photo must be
                          rewritten (default 95 — near-lossless). Not used otherwise.

By default each image is ORIENTED before upload (see orient_image): the EXIF orientation is
baked into the pixels so it doesn't appear rotated after MMS transcoding strips the metadata.
Photos are kept at their ORIGINAL SIZE — no downscaling, no recompression. Images that are
already upright are uploaded byte-for-byte untouched; only ones that actually need rotating are
re-encoded (at high quality). Pass --no-orient to upload the original untouched regardless.

Requires boto3 and Pillow (`pip install boto3 Pillow`); R2 is S3-compatible.
"""
import argparse
import io
import mimetypes
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # standing project rule: load .env before reading os.environ


def _client():
    """Build an S3 client pointed at R2. Raises SystemExit if config is missing."""
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("R2_ENDPOINT_URL")
    account_id = os.environ.get("R2_ACCOUNT_ID")
    if not endpoint and account_id:
        endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    missing = [n for n, v in (("R2_ENDPOINT_URL or R2_ACCOUNT_ID", endpoint),
                              ("R2_ACCESS_KEY_ID", access_key),
                              ("R2_SECRET_ACCESS_KEY", secret_key),
                              ("R2_BUCKET", os.environ.get("R2_BUCKET"))) if not v]
    if missing:
        raise SystemExit("Missing env var(s): " + ", ".join(missing) +
                         " — set them in .env (see this file's docstring).")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",                       # R2 expects "auto"
        config=Config(signature_version="s3v4"),  # required for R2 presigned URLs
    )


def orient_image(path, quality=None):
    """Return (bytes, content_type, suffix) with EXIF orientation baked into the pixels.

    Original dimensions are preserved — NO downscaling, NO recompression of the size. The
    only reason to touch the file is rotation: MMS transcoding strips the EXIF orientation
    tag, so a portrait photo that relied on it would show sideways. We therefore rotate the
    actual pixels and drop the tag.

    - Already upright (orientation tag 1 or absent) -> the ORIGINAL file is returned
      byte-for-byte (zero re-encoding, truly original).
    - Needs rotating -> re-encoded at its original size. PNGs stay lossless PNG; everything
      else is written as high-quality JPEG (R2_IMAGE_QUALITY, default 95 — visually lossless).
    """
    from PIL import Image, ImageOps

    path = Path(path)
    with Image.open(path) as opened:
        orientation = opened.getexif().get(0x0112)   # 0x0112 = EXIF Orientation tag
        if orientation in (None, 1):                 # already upright: pass through untouched
            ct = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return path.read_bytes(), ct, path.suffix.lower()
        fmt = (opened.format or "JPEG").upper()
        img = ImageOps.exif_transpose(opened)        # rotate the pixels, drop the tag
        buf = io.BytesIO()
        if fmt == "PNG":
            img.save(buf, format="PNG", optimize=True)          # lossless, original size
            return buf.getvalue(), "image/png", ".png"
        quality = int(quality or os.environ.get("R2_IMAGE_QUALITY", "95"))
        img.convert("RGB").save(buf, format="JPEG", quality=quality)  # original size, hi-Q
        return buf.getvalue(), "image/jpeg", ".jpg"


def upload_and_presign(path, ttl=None, key_prefix="mms/", orient=True, quality=None) -> str:
    """Upload a local image to R2 and return a presigned GET url (expires after `ttl` secs).

    By default the image is oriented (orient_image) but kept at its original size; pass
    orient=False to upload the original bytes untouched regardless of rotation. The object key
    is randomized so the (temporarily public) url is unguessable. Returns the url string.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"no such file: {path}")
    ttl = int(ttl or os.environ.get("R2_PRESIGN_TTL", "3600"))
    bucket = os.environ["R2_BUCKET"]

    if orient:
        body, content_type, suffix = orient_image(path, quality=quality)
    else:
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        suffix = path.suffix.lower()
    key = f"{key_prefix}{uuid.uuid4().hex}{suffix}"

    s3 = _client()
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=ttl)


def main():
    ap = argparse.ArgumentParser(
        description="Upload an image to Cloudflare R2 and print a presigned URL")
    ap.add_argument("path", help="local image file to upload")
    ap.add_argument("--ttl", type=int, default=None,
                    help="presigned URL lifetime in seconds (default R2_PRESIGN_TTL or 3600)")
    ap.add_argument("--no-orient", action="store_true",
                    help="upload the original file untouched (do not even bake in orientation)")
    ap.add_argument("--quality", type=int, default=None,
                    help="JPEG quality for the re-encode when a rotated photo must be rewritten "
                         "(default R2_IMAGE_QUALITY or 95); ignored for upright photos")
    args = ap.parse_args()
    try:
        url = upload_and_presign(args.path, ttl=args.ttl, orient=not args.no_orient,
                                 quality=args.quality)
    except Exception as e:
        sys.exit(f"upload failed: {type(e).__name__}: {e}")
    print(url)  # ONLY the url on stdout, so `URL=$(python r2_upload.py dog.jpg)` works


if __name__ == "__main__":
    main()
