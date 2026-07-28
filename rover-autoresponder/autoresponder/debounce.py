"""Per-thread debouncer: coalesce a burst of messages into ONE draft call.

When several messages for the same thread arrive close together (Rover's auto
"Boarding Request" block + the client's freeform note, or a client sending two
quick messages), we don't want a draft per message. Each arrival bump()s the
thread's deadline; once a thread has been quiet for `interval` seconds, on_fire
runs once with the thread's full accumulated history.

In-memory only: pending timers are lost on restart, which just means a message
that landed seconds before a restart won't be auto-drafted. Acceptable for v1.
"""
import logging
import threading
import time

log = logging.getLogger(__name__)


class Debouncer:
    def __init__(self, interval_sec, on_fire, tick_sec: float = 1.0):
        self.interval = interval_sec
        self.on_fire = on_fire          # callable(thread_key)
        self.tick = tick_sec
        self._deadlines = {}            # thread_key -> monotonic deadline
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def bump(self, thread_key: str) -> None:
        """Register/extend the quiet window for a thread."""
        if self.interval <= 0:
            self.on_fire(thread_key)    # debounce disabled -> fire immediately
            return
        with self._lock:
            self._deadlines[thread_key] = time.monotonic() + self.interval

    def start(self):
        self._thread = threading.Thread(target=self._run, name="debouncer", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            time.sleep(self.tick)
            self._fire_due()

    def _fire_due(self):
        now = time.monotonic()
        with self._lock:
            due = [tk for tk, dl in self._deadlines.items() if dl <= now]
            for tk in due:
                del self._deadlines[tk]
        for tk in due:
            try:
                self.on_fire(tk)
            except Exception:
                log.exception("debounced draft failed for thread %s", tk)

    def flush(self):
        """Fire every pending thread now (tests / graceful shutdown)."""
        with self._lock:
            due = list(self._deadlines.keys())
            self._deadlines.clear()
        for tk in due:
            self.on_fire(tk)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._deadlines)
