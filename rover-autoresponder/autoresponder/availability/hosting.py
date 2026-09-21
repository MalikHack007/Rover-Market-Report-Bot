"""Addendum D / D2 — durable PUBLIC R2 hosting for the availability page.

Unlike the MMS path (photos/hosting.py), these objects are served DIRECTLY over a public
custom domain — NOT via short-TTL presigned URLs. A client bookmarks/reopens the page, so
the URL must never expire. Two consequences, both deliberate:

  1. The feed uses a SEPARATE, public-read bucket (AVAIL_R2_BUCKET). The MMS bucket stays
     private (presigned + deleted after delivery); we must never make it public.
  2. We PUT objects at STABLE keys (no uuid, no TTL) and overwrite them each refresh.

Making the bucket *publicly served* is a one-time Cloudflare/R2 step (bind AVAIL_PUBLIC_BASE_URL
as a custom domain on the bucket, public GET only — not LIST). This module just writes objects.

R2 *account* credentials have their canonical home in photos/config.py (the first R2 user);
we reuse them and only add the availability bucket + public base URL.
"""
import json
import logging

from .. import config
from ..photos import config as r2creds

log = logging.getLogger(__name__)

JSON_CACHE = "public, max-age=300"     # short: clients pick up refreshes; CDN still absorbs load
PAGE_CACHE = "public, max-age=3600"    # the page changes rarely


def _client():
    """S3 client pointed at R2, using the shared account creds + the availability bucket.
    Raises RuntimeError if the public-hosting config is incomplete."""
    import boto3
    from botocore.config import Config

    endpoint = r2creds.R2_ENDPOINT_URL or (
        f"https://{r2creds.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
        if r2creds.R2_ACCOUNT_ID else "")
    missing = [n for n, v in (
        ("R2 endpoint/account", endpoint),
        ("R2_ACCESS_KEY_ID", r2creds.R2_ACCESS_KEY_ID),
        ("R2_SECRET_ACCESS_KEY", r2creds.R2_SECRET_ACCESS_KEY),
        ("AVAIL_R2_BUCKET", config.AVAIL_R2_BUCKET)) if not v]
    if missing:
        raise RuntimeError("availability R2 not configured — missing: " + ", ".join(missing))
    return boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=r2creds.R2_ACCESS_KEY_ID,
        aws_secret_access_key=r2creds.R2_SECRET_ACCESS_KEY,
        region_name="auto", config=Config(signature_version="s3v4"))


def public_url(key):
    """The public URL a client hits, from AVAIL_PUBLIC_BASE_URL + key."""
    base = (config.AVAIL_PUBLIC_BASE_URL or "").rstrip("/")
    return f"{base}/{key.lstrip('/')}" if base else key


def put_public(key, body, content_type, cache_control=JSON_CACHE):
    """PUT one object to the public bucket at a STABLE key (no presign, no TTL). Returns its
    public URL. Raises on failure — the publisher's callback logs it and keeps the local file."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    _client().put_object(Bucket=config.AVAIL_R2_BUCKET, Key=key, Body=body,
                         ContentType=content_type, CacheControl=cache_control)
    return public_url(key)


def publish_feed(payload):
    """publish callback for publisher.run_once: upload availability.json to the public bucket."""
    url = put_public(config.AVAIL_R2_PUBLIC_KEY_JSON,
                     json.dumps(payload, indent=2),
                     "application/json; charset=utf-8", cache_control=JSON_CACHE)
    log.info("availability.json published: %s", url)
    return url


def publish_page(html):
    """Upload index.html (D3). Called once / when the page changes, not every refresh."""
    url = put_public(config.AVAIL_R2_PUBLIC_KEY_HTML, html,
                     "text/html; charset=utf-8", cache_control=PAGE_CACHE)
    log.info("availability page published: %s", url)
    return url


def feed_publisher_or_none():
    """Return the publish_feed callback if the public bucket is configured, else None — so
    the publisher runs in D1-only mode (write the local file, skip the upload) until R2 is set."""
    if config.AVAIL_R2_BUCKET and config.AVAIL_PUBLIC_BASE_URL:
        return publish_feed
    log.info("availability R2 upload disabled — set AVAIL_R2_BUCKET + AVAIL_PUBLIC_BASE_URL "
             "to publish the feed to the public page")
    return None
