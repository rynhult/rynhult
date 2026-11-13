"""Short, process-local leases from signed-in browser tabs; no heartbeat database writes."""
from __future__ import annotations

import threading
import time
from collections import deque


class Presence:
    def __init__(self):
        self.tabs = {}
        self._requests = {}
        self._lock = threading.Lock()

    def update(self, username, login, tab, scope, idle_ms, active, sequence=0):
        with self._lock:
            stamp = time.monotonic()
            holding = active and idle_ms < 120_000 and (scope == '/inbox' or scope.startswith('/sessions/'))
            # Releases must still clear a lease when active heartbeats exhausted the budget.
            if holding:
                self._requests = {person: times for person, times in self._requests.items()
                                  if times and times[-1] > stamp - 15}
                recent = self._requests.setdefault(username, deque())
                while recent and recent[0] <= stamp - 15:
                    recent.popleft()
                if len(recent) >= 30:
                    return False
                recent.append(stamp)
            self.tabs = {key: value for key, value in self.tabs.items() if value[1] > stamp}
            key = (username, login, tab)
            if key in self.tabs and sequence and sequence <= self.tabs[key][2]:
                return True
            if key not in self.tabs:
                own = [entry for entry in self.tabs if entry[:2] == (username, login)]
                if len(own) >= 16:
                    # Dict insertion order is the admission order, not client-controlled time.
                    self.tabs.pop(own[0])
            if holding:
                self.tabs[key] = (scope, stamp + min(45, (120_000 - idle_ms) / 1000), sequence)
            else:
                self.tabs[key] = (None, stamp + 45, sequence)
            return True

    def watching(self, username, session_id):
        with self._lock:
            stamp = time.monotonic()
            return any(person == username and expires > stamp and
                       (scope == '/inbox' or (session_id and scope == f'/sessions/{session_id}'))
                       for (person, _, _), (scope, expires, _) in self.tabs.items())
