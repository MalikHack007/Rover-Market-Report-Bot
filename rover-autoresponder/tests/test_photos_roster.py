"""Addendum C: the photo-update roster (dogs in custody today).

Regression for the ±1-day-grace bug (2026-08-31): a stay that ended yesterday or starts
tomorrow must NOT appear; the bounds are inclusive so same-day drop-off/pick-up still do.
"""
import datetime

from autoresponder import store
from autoresponder.photos import store as pstore

TODAY = datetime.date(2026, 8, 31)


def _book(conn, num, pet, stay, status="converted", booked=True):
    store.upsert_sms_thread(conn, num, owner_name="Owner " + pet, pet_name=pet,
                            stay_dates=stay, status=status)
    if booked:
        store.mark_has_booked(conn, num)


def test_roster_inclusive_bounds_no_grace(tmp_path):
    conn = store.init_db(str(tmp_path / "roster.db"))
    _book(conn, "+1", "MidStay",    "08/18/2026 to 09/01/2026")   # in
    _book(conn, "+2", "LastDay",    "08/28/2026 to 08/31/2026")   # in (end == today)
    _book(conn, "+3", "FirstDay",   "08/31/2026 to 09/03/2026")   # in (start == today)
    _book(conn, "+4", "DayCare",    "2026-08-31 to 2026-08-31")   # in (start == end == today)
    _book(conn, "+5", "EndedYest",  "08/25/2026 to 08/30/2026")   # OUT (ended yesterday)
    _book(conn, "+6", "StartsTmrw", "09/01/2026 to 09/06/2026")   # OUT (starts tomorrow)

    pets = {e["pet"] for e in pstore.list_active_bookings(conn, today=TODAY)}
    assert pets == {"MidStay", "LastDay", "FirstDay", "DayCare"}, pets


def test_roster_excludes_non_confirmed_and_unbooked(tmp_path):
    conn = store.init_db(str(tmp_path / "roster2.db"))
    _book(conn, "+7", "Screening", "08/30/2026 to 09/02/2026", status="active")   # not converted
    _book(conn, "+8", "NeverBooked", "08/30/2026 to 09/02/2026", booked=False)    # has_booked=0
    _book(conn, "+9", "NoDates", None)                                            # unparseable
    _book(conn, "+10", "Good", "08/30/2026 to 09/02/2026")                        # the only valid one

    pets = {e["pet"] for e in pstore.list_active_bookings(conn, today=TODAY)}
    assert pets == {"Good"}, pets
