import time

from autoresponder.debounce import Debouncer


def test_zero_interval_fires_immediately():
    fired = []
    d = Debouncer(0, on_fire=fired.append)
    d.bump("t1")
    assert fired == ["t1"]


def test_multiple_bumps_same_thread_coalesce_to_one_fire():
    fired = []
    d = Debouncer(60, on_fire=fired.append)          # long window; we flush manually
    d.bump("t1"); d.bump("t1"); d.bump("t1")         # burst of 3 for one thread
    assert d.pending_count() == 1                    # only one pending entry
    d.flush()
    assert fired == ["t1"]                            # ONE draft call, not three


def test_distinct_threads_fire_once_each():
    fired = []
    d = Debouncer(60, on_fire=fired.append)
    d.bump("a"); d.bump("b"); d.bump("a")
    d.flush()
    assert sorted(fired) == ["a", "b"]


def test_background_timer_fires_after_window_not_before():
    fired = []
    d = Debouncer(0.15, on_fire=fired.append, tick_sec=0.02).start()
    try:
        d.bump("t1")
        time.sleep(0.05)
        assert fired == []            # still inside the window
        time.sleep(0.25)
        assert fired == ["t1"]        # fired once the window elapsed
    finally:
        d.stop()


def test_bump_extends_window():
    fired = []
    d = Debouncer(0.15, on_fire=fired.append, tick_sec=0.02).start()
    try:
        d.bump("t1")
        time.sleep(0.10)
        d.bump("t1")                  # extend before it fires
        time.sleep(0.10)
        assert fired == []            # would have fired at 0.15 without the 2nd bump
        time.sleep(0.20)
        assert fired == ["t1"]        # fires once, after the extended window
    finally:
        d.stop()
