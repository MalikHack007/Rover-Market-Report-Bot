"""Delivery-status poller (Addendum C, P2) — batched, budget-safe.

Runs as a daemon thread inside rover-sms. While any dog-update is `sent` but not yet terminal,
it does ONE batched Telerivet query per cycle (not one call per message), flips each row to
`delivered`/`failed`, alerts on failure, and tears down the R2 objects. It makes ZERO API calls
when nothing is pending, backs off nothing fancy (a fixed short interval while a batch settles),
gives up on stragglers after a window (MMS receipts are flaky), and reserves the daily API
budget for sends so 200/day is never hit. See §8.3.
"""
import logging
import time
from datetime import datetime

from .. import store as base
from . import store as pstore, config
from . import telegram as ui
from .hosting import delete as r2_delete
from .telerivet import TelerivetClient, DELIVERED, FAILED

log = logging.getLogger(__name__)


def _age_minutes(updated_at):
    try:
        return (datetime.utcnow() - datetime.fromisoformat(updated_at)).total_seconds() / 60.0
    except (ValueError, TypeError):
        return 0.0


def _teardown(conn, update_id):
    """Delete the R2 objects + staged local files for a finalized update (privacy/cleanup)."""
    import os
    for _mid, _fid, local_path, r2_key, _pos in pstore.get_media(conn, update_id):
        if r2_key:
            r2_delete(r2_key)
        if local_path:
            try:
                os.remove(local_path)
            except OSError:
                pass


def _alert_failed(conn, update_id):
    row = pstore.get_update(conn, update_id)
    pet = row[4] if row else None
    trow = base.get_thread(conn, row[2]) if row else None
    owner = trow[0] if trow else None
    ui.send(f"⚠️ The photo update to <b>{ui.esc(owner) or 'a client'}</b> "
            f"({ui.esc(pet) or 'their dog'}) <b>failed to deliver</b>. It did not reach them.")


def poll_once(conn, gw=None) -> int:
    """One cycle: give up on stale sends, then a single batched status query for the rest.
    Returns how many updates were finalized this cycle (delivered/failed/given-up)."""
    pending = pstore.list_pending_sent(conn)      # (id, telerivet_msg_id, updated_at)
    if not pending:
        return 0

    finalized = 0
    # 1) Give up on stragglers (no API call): mark "unconfirmed" and tear down.
    fresh = []
    for update_id, tid, updated_at in pending:
        if _age_minutes(updated_at) > config.PHOTO_GIVE_UP_MINUTES:
            pstore.set_status(conn, update_id, "unconfirmed")
            _teardown(conn, update_id)
            finalized += 1
            log.info("giving up on delivery confirmation for update %s (sent, unconfirmed)",
                     update_id)
        else:
            fresh.append((update_id, tid))
    if not fresh:
        return finalized

    # 2) Budget guard: reserve headroom for sends — stop polling before the ceiling.
    if pstore.api_calls_today(conn) >= config.TELERIVET_DAILY_POLL_BUDGET:
        log.warning("poll budget reached (%d) — leaving %d updates unconfirmed for now",
                    config.TELERIVET_DAILY_POLL_BUDGET, len(fresh))
        return finalized

    # 3) ONE batched query for all outstanding statuses (newest first).
    gw = gw or TelerivetClient()
    try:
        msgs = gw.query_messages(sort_dir="desc")
    except Exception:
        log.exception("telerivet delivery-status poll failed (will retry next cycle)")
        return finalized
    pstore.bump_api_calls(conn, 1)
    status_by_id = {m["id"]: m["status"] for m in msgs if m.get("id")}

    for update_id, tid in fresh:
        st = status_by_id.get(tid)
        if st in DELIVERED:
            pstore.set_status(conn, update_id, "delivered")
            _teardown(conn, update_id)
            finalized += 1
            log.info("DELIVERED photo update %s", update_id)
        elif st in FAILED:
            pstore.set_status(conn, update_id, "failed")
            _alert_failed(conn, update_id)
            _teardown(conn, update_id)
            finalized += 1
            log.error("photo update %s FAILED to deliver", update_id)
        # else still queued/sent -> leave it for the next cycle
    return finalized


def poll_loop(conn, stop_event=None):
    interval = config.PHOTO_POLL_INTERVAL_SEC
    log.info("photo delivery-status poller active (%ss while a batch settles)", interval)
    while not (stop_event and stop_event.is_set()):
        try:
            # Checking for pending work is a local DB read — zero API calls when idle.
            if pstore.list_pending_sent(conn):
                poll_once(conn)
        except Exception:
            log.exception("photo poller cycle error")
        time.sleep(interval)
