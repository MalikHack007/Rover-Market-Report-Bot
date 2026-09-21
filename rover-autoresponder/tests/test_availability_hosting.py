"""Addendum D / D2 — durable public R2 hosting for the availability feed."""
import json

import pytest

from autoresponder import config
from autoresponder.availability import hosting


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)
        return {"ETag": "x"}


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(hosting, "_client", lambda: fake)
    monkeypatch.setattr(config, "AVAIL_R2_BUCKET", "rover-availability-public")
    monkeypatch.setattr(config, "AVAIL_PUBLIC_BASE_URL", "https://availability.example.com/")
    monkeypatch.setattr(config, "AVAIL_R2_PUBLIC_KEY_JSON", "availability/availability.json")
    monkeypatch.setattr(config, "AVAIL_R2_PUBLIC_KEY_HTML", "availability/index.html")
    return fake


def test_public_url_joins_base_and_key(monkeypatch):
    monkeypatch.setattr(config, "AVAIL_PUBLIC_BASE_URL", "https://availability.example.com/")
    assert hosting.public_url("availability/availability.json") == \
        "https://availability.example.com/availability/availability.json"
    # no double slash even if the key has a leading one
    assert hosting.public_url("/a/b.json") == "https://availability.example.com/a/b.json"


def test_put_public_uploads_at_stable_key_with_cache_and_type(s3):
    url = hosting.put_public("availability/availability.json", '{"x":1}',
                             "application/json; charset=utf-8")
    assert url == "https://availability.example.com/availability/availability.json"
    put = s3.puts[0]
    assert put["Bucket"] == "rover-availability-public"
    assert put["Key"] == "availability/availability.json"          # stable, no uuid
    assert put["ContentType"] == "application/json; charset=utf-8"
    assert put["CacheControl"] == hosting.JSON_CACHE
    assert put["Body"] == b'{"x":1}'                                # str encoded to bytes


def test_publish_feed_serializes_payload_to_json_key(s3):
    payload = {"generated_at": "2026-09-20T14:05:00Z", "days": {"2026-09-20": True}}
    url = hosting.publish_feed(payload)
    assert url.endswith("/availability/availability.json")
    put = s3.puts[0]
    assert put["Key"] == "availability/availability.json"
    assert json.loads(put["Body"].decode())["days"]["2026-09-20"] is True


def test_publish_page_uses_html_key_and_longer_cache(s3):
    hosting.publish_page("<!doctype html><title>x</title>")
    put = s3.puts[0]
    assert put["Key"] == "availability/index.html"
    assert put["ContentType"] == "text/html; charset=utf-8"
    assert put["CacheControl"] == hosting.PAGE_CACHE


def test_feed_publisher_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "AVAIL_R2_BUCKET", "")
    monkeypatch.setattr(config, "AVAIL_PUBLIC_BASE_URL", "")
    assert hosting.feed_publisher_or_none() is None


def test_feed_publisher_callback_when_configured(s3):
    cb = hosting.feed_publisher_or_none()
    assert cb is hosting.publish_feed


def test_missing_config_raises_clear_error(monkeypatch):
    # _client() must name what's missing rather than fail obscurely inside boto3.
    from autoresponder.photos import config as r2creds
    monkeypatch.setattr(r2creds, "R2_ACCOUNT_ID", "")
    monkeypatch.setattr(r2creds, "R2_ENDPOINT_URL", "")
    monkeypatch.setattr(r2creds, "R2_ACCESS_KEY_ID", "")
    monkeypatch.setattr(r2creds, "R2_SECRET_ACCESS_KEY", "")
    monkeypatch.setattr(config, "AVAIL_R2_BUCKET", "")
    with pytest.raises(RuntimeError) as e:
        hosting._client()
    assert "AVAIL_R2_BUCKET" in str(e.value)
