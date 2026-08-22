#!/usr/bin/env python3
"""Live test of the scheduling path — real calendar, real Cal.com links.

Simulates a booking confirmation without needing a real Rover booking, then lets you
watch the whole scheduling loop:

    python3 live_scheduling_test.py            # 1. create PENDING events + links
    (open a link, book a slot)
    python3 live_scheduling_test.py --poll     # 2. flip it to CONFIRMED
    python3 live_scheduling_test.py --cleanup  # 3. delete everything it made

Uses a dedicated fake thread key (+19995550000) so it can't touch real client data.
Telegram is NOT involved — output is printed here.
"""
import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from autoresponder import config, store                                   # noqa: E402
from autoresponder.calendar_client import GoogleCalendar                  # noqa: E402
from autoresponder.scheduling import (                                    # noqa: E402
    DROPOFF, PICKUP, build_scheduling_draft, on_booking_confirmed,
)

TEST_THREAD = "+19995550000"
TEST_OWNER = "TestOwner"
TEST_PET = "TestDog"


def preflight():
    problems = []
    if not config.GOOGLE_CALENDAR_ID:
        problems.append("GOOGLE_CALENDAR_ID is not set (.env)")
    if not config.CALCOM_USERNAME:
        problems.append("CALCOM_USERNAME is not set (.env)")
    if problems:
        print("Cannot run:")
        for p in problems:
            print("  •", p)
        sys.exit(1)
    print(f"calendar : {config.GOOGLE_CALENDAR_ID}")
    print(f"cal.com  : {config.CALCOM_BASE_URL}/{config.CALCOM_USERNAME}")
    print(f"database : {config.DB_PATH}")


def create(conn, days_out):
    start = date.today() + timedelta(days=days_out)
    end = start + timedelta(days=2)
    print(f"\nSimulating: '{TEST_OWNER} has confirmed a booking request (stay) with "
          f"{TEST_PET} from {start:%m/%d} to {end:%m/%d}'\n")

    store.upsert_sms_thread(conn, TEST_THREAD, owner_name=TEST_OWNER,
                            pet_name=TEST_PET, status="converted")
    created = on_booking_confirmed(conn, TEST_THREAD, TEST_PET,
                                   start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y"))
    if not created:
        print("No calendar events were created — check the errors above.")
        return

    print(f"\n✅ {len(created)} PENDING event(s) created on your ROVER calendar.")
    print("   Look for them now — they should be transparent (free) blocks.\n")

    text, links = build_scheduling_draft(conn, TEST_THREAD)
    if not text:
        print("⚠️  Links could not be built (check CALCOM_* settings).")
        return
    print("─" * 74)
    print("THE MESSAGE THE CLIENT WOULD GET (approve-and-send would deliver this):")
    print("─" * 74)
    print(text)
    print("─" * 74)
    print("\nNEXT: open the drop-off link, book a slot, then run:")
    print("      python3 live_scheduling_test.py --poll")


def show(conn):
    rows = store.list_scheduling_events(conn, thread_key=TEST_THREAD)
    if not rows:
        print("\n(no test scheduling events in the database)")
        return rows
    print(f"\n{'kind':<10} {'status':<10} {'target':<12} {'booked at':<22} gcal id")
    for r in rows:
        print(f"{r[3]:<10} {r[5]:<10} {r[6] or '—':<12} {str(r[7] or '—'):<22} {r[8]}")
    return rows


def poll(conn):
    from autoresponder.calcom_client import CalcomClient
    from autoresponder.calcom_poller import poll_once

    if not config.CALCOM_API_KEY:
        print("CALCOM_API_KEY is not set — the poller can't read your bookings.")
        sys.exit(1)
    print("\nBefore:")
    show(conn)
    n = poll_once(conn, client=CalcomClient(), calendar=GoogleCalendar())
    print(f"\npoller applied {n} change(s)")
    print("\nAfter:")
    show(conn)
    if n:
        print("\n✅ Check the calendar: the event should have MOVED to your booked time,\n"
              "   been retitled (CONFIRMED), and turned opaque (busy).")
    else:
        print("\nNothing changed. Either no booking was made yet, or the booking didn't\n"
              "match — run with --debug-bookings to see what Cal.com returned.")


def debug_bookings():
    from autoresponder.calcom_client import CalcomClient
    for b in CalcomClient().list_bookings()[:5]:
        print(f"  {b['id']:<26} {b['event_type_slug']:<12} {b['start']}  "
              f"status={b['status']:<10} ref={b['ref']}")


def cleanup(conn):
    rows = store.list_scheduling_events(conn, thread_key=TEST_THREAD)
    cal = GoogleCalendar()
    for r in rows:
        if r[8]:
            cal.delete_event(r[8])
    with store._LOCK, conn:
        conn.execute("DELETE FROM scheduling_events WHERE thread_key=?", (TEST_THREAD,))
        conn.execute("DELETE FROM messages WHERE thread_key=?", (TEST_THREAD,))
        conn.execute("DELETE FROM threads WHERE thread_key=?", (TEST_THREAD,))
    print(f"\nDeleted {len(rows)} calendar event(s) and the test thread.")
    print("If you made a Cal.com booking, cancel it there too.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", action="store_true", help="check Cal.com for a booked slot")
    ap.add_argument("--show", action="store_true", help="show current test events")
    ap.add_argument("--cleanup", action="store_true", help="delete everything this made")
    ap.add_argument("--debug-bookings", action="store_true",
                    help="dump recent Cal.com bookings")
    ap.add_argument("--days-out", type=int, default=14,
                    help="how far ahead the fake booking starts (default 14)")
    args = ap.parse_args()

    preflight()
    conn = store.init_db(config.DB_PATH)

    if args.cleanup:
        cleanup(conn)
    elif args.poll:
        poll(conn)
    elif args.show:
        show(conn)
    elif args.debug_bookings:
        debug_bookings()
    else:
        if store.list_scheduling_events(conn, thread_key=TEST_THREAD):
            print("\nTest events already exist — run --cleanup first, or --poll to check "
                  "for a booking.")
            show(conn)
            return
        create(conn, args.days_out)


if __name__ == "__main__":
    main()
