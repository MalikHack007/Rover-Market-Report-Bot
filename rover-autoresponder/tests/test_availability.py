"""Addendum D / D1 — availability feed: slot reduction, the JSON contract, and the
self-healing refresh cycle."""
import json
from datetime import date, datetime, timezone

import pytest

from autoresponder import config
from autoresponder.calcom_client import CalcomClient, TransientCalcomError, slots_by_day
from autoresponder.availability import publisher

TZ = "America/Chicago"


class FakeCalcom(CalcomClient):
    """A CalcomClient whose slots payload is canned — exercises the REAL slots_by_day /
    available_days reduction, not a reimplementation of it."""

    def __init__(self, payload=None, raise_transient=False):
        super().__init__(api_key="fake")
        self._payload = payload if payload is not None else {}
        self._raise = raise_transient
        self.calls = []

    def available_slots_payload(self, event_type_id, start_day, end_day, tz):
        self.calls.append((event_type_id, start_day, end_day, tz))
        if self._raise:
            raise TransientCalcomError("boom")
        return self._payload


# --- slots_by_day: defensive across Cal.com's response shapes -------------------------

def test_slots_by_day_shape_wrapped_dict():
    # Shape A: data.slots = date-keyed dict of slot lists.
    payload = {"data": {"slots": {
        "2026-09-20": [{"start": "x"}, {"start": "y"}],
        "2026-09-22": [],            # present but empty => 0 (blocked)
    }}}
    assert slots_by_day(payload, TZ) == {"2026-09-20": 2, "2026-09-22": 0}


def test_slots_by_day_shape_unwrapped_dict():
    # Shape B: data itself is the date-keyed dict.
    payload = {"data": {"2026-09-20": [{"start": "x"}], "2026-09-21": [{"a": 1}, {"b": 2}]}}
    assert slots_by_day(payload, TZ) == {"2026-09-20": 1, "2026-09-21": 2}


def test_slots_by_day_shape_flat_list_buckets_by_local_day():
    # Shape C: flat list of slot objects with UTC timestamps. 02:00Z on the 21st is still
    # the evening of the 20th in America/Chicago — must bucket to 2026-09-20 (Addendum B).
    payload = {"data": [
        {"start": "2026-09-21T02:00:00Z"},
        {"start": "2026-09-21T15:00:00Z"},
    ]}
    counts = slots_by_day(payload, TZ)
    assert counts.get("2026-09-20") == 1
    assert counts.get("2026-09-21") == 1


def test_slots_by_day_empty_payload():
    assert slots_by_day({}, TZ) == {}
    assert slots_by_day({"data": {}}, TZ) == {}


# --- available_days: every day gets an explicit boolean -------------------------------

def test_available_days_fills_every_day_true_false():
    payload = {"data": {"slots": {
        "2026-09-20": [{"s": 1}],   # available
        "2026-09-21": [{"s": 1}],   # available
        # 09-22 absent => blocked
        "2026-09-23": [],           # present-empty => blocked
    }}}
    client = FakeCalcom(payload)
    days = client.available_days("123", date(2026, 9, 20), date(2026, 9, 23), TZ)
    assert days == {
        "2026-09-20": True,
        "2026-09-21": True,
        "2026-09-22": False,
        "2026-09-23": False,
    }


# --- build_payload: the exact contract the frontend reads -----------------------------

def test_build_payload_contract():
    now = datetime(2026, 9, 20, 14, 5, 0, tzinfo=timezone.utc)
    days = {"2026-09-21": True, "2026-09-20": False}  # deliberately unsorted
    payload = publisher.build_payload(days, now, TZ, 90)
    assert payload["generated_at"] == "2026-09-20T14:05:00Z"
    assert payload["timezone"] == TZ
    assert payload["horizon_days"] == 90
    assert payload["default"] == "unknown"
    # keys sorted for a stable diff
    assert list(payload["days"].keys()) == ["2026-09-20", "2026-09-21"]
    # round-trips as valid JSON
    assert json.loads(json.dumps(payload))["days"]["2026-09-21"] is True


def test_refresh_uses_local_today_and_horizon(monkeypatch):
    monkeypatch.setattr(config, "AVAIL_PROBE_EVENT_TYPE_ID", "123")
    client = FakeCalcom({"data": {"slots": {}}})
    # 03:00Z on the 21st is the 20th, locally — refresh must probe from the LOCAL date.
    now = datetime(2026, 9, 21, 3, 0, 0, tzinfo=timezone.utc)
    payload = publisher.refresh(client=client, now=now, tz=TZ, horizon_days=30)
    et, start_day, end_day, tz = client.calls[0]
    assert start_day == date(2026, 9, 20)
    assert (end_day - start_day).days == 30
    assert payload["horizon_days"] == 30
    # empty slots => every day blocked
    assert all(v is False for v in payload["days"].values())


# --- run_once: writes the file, and never publishes an all-blocked lie on outage -------

def test_run_once_writes_json(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AVAIL_PROBE_EVENT_TYPE_ID", "123")
    out = tmp_path / "availability.json"
    payload = {"data": {"slots": {"2026-09-20": [{"s": 1}]}}}
    result = publisher.run_once(client=FakeCalcom(payload), path=str(out))
    assert result is not None
    on_disk = json.loads(out.read_text())
    assert on_disk["days"]  # non-empty
    assert set(on_disk) == {"generated_at", "timezone", "horizon_days", "default", "days"}


def test_run_once_keeps_last_good_on_outage(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AVAIL_PROBE_EVENT_TYPE_ID", "123")
    out = tmp_path / "availability.json"
    out.write_text('{"days": {"2026-09-20": true}, "sentinel": "last-good"}')
    result = publisher.run_once(client=FakeCalcom(raise_transient=True), path=str(out))
    assert result is None                       # skipped, not published
    assert json.loads(out.read_text())["sentinel"] == "last-good"  # untouched


def test_run_once_invokes_publish_callback(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AVAIL_PROBE_EVENT_TYPE_ID", "123")
    out = tmp_path / "availability.json"
    seen = []
    publisher.run_once(client=FakeCalcom({"data": {"slots": {}}}), path=str(out),
                       publish=lambda p: seen.append(p))
    assert len(seen) == 1 and "days" in seen[0]


def test_run_once_survives_publish_failure(tmp_path, monkeypatch):
    # An upload (D2) blowing up must not lose the locally-written feed nor crash the cycle.
    monkeypatch.setattr(config, "AVAIL_PROBE_EVENT_TYPE_ID", "123")
    out = tmp_path / "availability.json"

    def boom(_):
        raise RuntimeError("r2 down")

    result = publisher.run_once(client=FakeCalcom({"data": {"slots": {}}}),
                                path=str(out), publish=boom)
    assert result is not None
    assert out.exists()


def test_start_thread_noop_without_probe(monkeypatch):
    monkeypatch.setattr(config, "AVAIL_PROBE_EVENT_TYPE_ID", "")
    assert publisher.start_thread() is None
