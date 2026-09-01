"""Regression: a second, overlapping booking on one Rover number (confirmed out of order)
must get its OWN episode instead of colliding with the prior booking's scheduling events +
links-sent flag.

The real case (Revanth, +18582631421, 2026-09): an earlier BOARDING inquiry (episode N) was
still open when a DAY-CARE was booked+confirmed as episode N+1, placing that episode's
drop-off/pick-up events. When the boarding finally confirmed, it reused episode N+1, whose
claim(thread, N+1, kind) silently no-op'd (rows already existed) and whose links_sent:N+1 was
already set — so no boarding events and no links were ever generated.
"""
from datetime import date

from autoresponder import store, scheduling
from autoresponder.scheduling import DROPOFF, PICKUP, PENDING, CANCELLED, on_booking_confirmed
from tests.test_scheduling import FakeCalendar

A = "+15125550009"
TODAY = date(2026, 8, 20)


def _events(conn, episode):
    return {k: store.get_scheduling_event(conn, A, episode, k) for k in (DROPOFF, PICKUP)}


def test_second_booking_gets_its_own_episode(tmp_path):
    conn = store.init_db(str(tmp_path / "ep.db"))
    store.upsert_sms_thread(conn, A, owner_name="Revanth", pet_name="Blue",
                            stay_dates="08/31/2026", status="converted")
    cal = FakeCalendar()

    # Booking #1 — a day-care on 08/31 — confirmed first, lands on episode 1.
    on_booking_confirmed(conn, A, "Blue", "08/31/2026", "08/31/2026", calendar=cal, today=TODAY)
    ep1 = store.get_episode(conn, A)
    assert _events(conn, ep1)[DROPOFF][2] == "2026-08-31"          # target_date

    # Booking #2 — a boarding 10/04–10/24 — confirmed later on the SAME (now occupied) episode.
    on_booking_confirmed(conn, A, "Blue", "10/04/2026", "10/24/2026", calendar=cal, today=TODAY)
    ep2 = store.get_episode(conn, A)

    assert ep2 == ep1 + 1, "boarding must advance to its own episode"
    # The day-care events are untouched on the old episode …
    assert _events(conn, ep1)[DROPOFF][2] == "2026-08-31"
    # … and the boarding got fresh drop-off (10/04) + pick-up (10/24) on the new episode.
    assert _events(conn, ep2)[DROPOFF][2] == "2026-10-04"
    assert _events(conn, ep2)[PICKUP][2] == "2026-10-24"
    assert _events(conn, ep2)[DROPOFF][1] == PENDING


def test_same_booking_reconfirm_does_not_bump(tmp_path):
    """The SMS marker and confirmation email fire for the SAME dates — the second must be a
    no-op, not a spurious new episode."""
    conn = store.init_db(str(tmp_path / "ep2.db"))
    store.upsert_sms_thread(conn, A, owner_name="Jess", pet_name="Archie", status="converted")
    cal = FakeCalendar()
    on_booking_confirmed(conn, A, "Archie", "09/01/2026", "09/06/2026", calendar=cal, today=TODAY)
    ep = store.get_episode(conn, A)
    on_booking_confirmed(conn, A, "Archie", "09/01/2026", "09/06/2026", calendar=cal, today=TODAY)
    assert store.get_episode(conn, A) == ep                        # unchanged
    assert len(cal.created) == 2                                   # no duplicate events


def test_cas_bump_is_once_even_if_detected_twice(tmp_path):
    """If two processes both read the old episode and both try to bump, the compare-and-swap
    lets exactly one bump land — the episode advances once, not twice."""
    conn = store.init_db(str(tmp_path / "ep3.db"))
    store.upsert_sms_thread(conn, A, status="converted")
    # occupy episode 1 with an unrelated stay
    store.claim_scheduling_event(conn, A, 1, DROPOFF, target_date="2026-08-31")
    e1 = store.start_new_booking_episode(conn, A, from_episode=1, stay_dates="x")
    e2 = store.start_new_booking_episode(conn, A, from_episode=1, stay_dates="x")   # stale retry
    assert e1 == 2 and e2 == 2                                     # both resolve to 2, no ep3


def test_cancelled_prior_booking_forces_new_episode(tmp_path):
    """A cancelled booking still holds the unique (thread, episode, kind) rows; a fresh booking
    on that episode would silently fail to claim — so it must get a new episode."""
    conn = store.init_db(str(tmp_path / "ep4.db"))
    store.upsert_sms_thread(conn, A, pet_name="Blue", status="converted")
    cal = FakeCalendar()
    on_booking_confirmed(conn, A, "Blue", "09/01/2026", "09/06/2026", calendar=cal, today=TODAY)
    ep = store.get_episode(conn, A)
    for k in (DROPOFF, PICKUP):
        ev = store.get_scheduling_event(conn, A, ep, k)
        store.update_scheduling_event(conn, ev[0], status=CANCELLED)
    # re-book the SAME dates after cancellation → must not collide with the cancelled rows
    on_booking_confirmed(conn, A, "Blue", "09/01/2026", "09/06/2026", calendar=cal, today=TODAY)
    ep_new = store.get_episode(conn, A)
    assert ep_new == ep + 1
    assert store.get_scheduling_event(conn, A, ep_new, DROPOFF)[1] == PENDING
