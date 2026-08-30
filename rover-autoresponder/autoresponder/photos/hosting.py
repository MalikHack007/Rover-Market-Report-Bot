"""Cloudflare R2 hosting for outbound MMS media.

Telerivet needs a PUBLIC url to fetch each photo, so we upload it to R2 and hand back a
short-TTL presigned url. Photos are kept at ORIGINAL SIZE — the only transform is baking in
EXIF orientation (MMS transcoding strips the tag and would otherwise rotate portrait photos).
Hardened from the proven ../../r2_upload.py PoC.
"""
import io
import mimetypes
import uuid
from pathlib import Path

from . import config


def _client():
    """S3 client pointed at R2. Raises RuntimeError if R2 isn't configured."""
    import boto3
    from botocore.config import Config

    endpoint = config.R2_ENDPOINT_URL or (
        f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
        if config.R2_ACCOUNT_ID else "")
    missing = [n for n, v in (("R2 endpoint/account", endpoint),
                              ("R2_ACCESS_KEY_ID", config.R2_ACCESS_KEY_ID),
                              ("R2_SECRET_ACCESS_KEY", config.R2_SECRET_ACCESS_KEY),
                              ("R2_BUCKET", config.R2_BUCKET)) if not v]
    if missing:
        raise RuntimeError("R2 not configured — missing: " + ", ".join(missing))
    return boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        region_name="auto", config=Config(signature_version="s3v4"))


def orient_bytes(path):
    """(bytes, content_type, suffix) with EXIF orientation baked in; ORIGINAL size kept.

    Upright photos (orientation tag 1 or absent) pass through byte-for-byte. Only rotated ones
    are re-encoded — PNGs losslessly, everything else as high-quality JPEG (R2_IMAGE_QUALITY).
    """
    from PIL import Image, ImageOps

    path = Path(path)
    with Image.open(path) as opened:
        if opened.getexif().get(0x0112) in (None, 1):     # already upright → untouched
            ct = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return path.read_bytes(), ct, path.suffix.lower()
        fmt = (opened.format or "JPEG").upper()
        img = ImageOps.exif_transpose(opened)              # rotate pixels, drop the tag
        buf = io.BytesIO()
        if fmt == "PNG":
            img.save(buf, format="PNG", optimize=True)
            return buf.getvalue(), "image/png", ".png"
        img.convert("RGB").save(buf, format="JPEG", quality=config.R2_IMAGE_QUALITY)
        return buf.getvalue(), "image/jpeg", ".jpg"


def upload(path, key_prefix="mms/", ttl=None):
    """Upload one photo to R2. Returns (presigned_url, r2_key). Key is randomized/unguessable."""
    body, content_type, suffix = orient_bytes(path)
    key = f"{key_prefix}{uuid.uuid4().hex}{suffix}"
    s3 = _client()
    s3.put_object(Bucket=config.R2_BUCKET, Key=key, Body=body, ContentType=content_type)
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": config.R2_BUCKET, "Key": key},
        ExpiresIn=int(ttl or config.R2_PRESIGN_TTL))
    return url, key


def delete(key):
    """Remove an object after delivery (privacy). Returns True on success."""
    if not key:
        return False
    try:
        _client().delete_object(Bucket=config.R2_BUCKET, Key=key)
        return True
    except Exception:
        return False
