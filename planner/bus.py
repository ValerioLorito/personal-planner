"""
bus.py — in-process publish/subscribe used to push live updates to the UI
over Server-Sent Events (and to let the agent traces stream while they run).
"""
from __future__ import annotations

import queue
import threading


class Bus:
    def __init__(self):
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self) -> "queue.Queue":
        q = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, kind: str, data=None) -> None:
        msg = {"kind": kind, "data": data or {}}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


bus = Bus()
