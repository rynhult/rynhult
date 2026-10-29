"""Unwired company execution-lease state machine; no I/O, threads or supervisor imports.

The caller supplies authenticated, freshly received policy authority and the remaining
policy lifetime (not its original TTL). It must poll check() at execution boundaries
and from an independent watchdog, and implement process termination/secret cleanup.
This prototype does not verify signatures or provide a scheduler. A stopped object
cannot restart work: reconciliation and a new compliant launch need a new instance.
"""
from __future__ import annotations

import math
from collections.abc import Callable


class ExecutionLease:
    def __init__(self, *, clock: Callable[[], float], stop: Callable[[str], None]):
        # Inject a monotonic, suspend-aware clock in a future broker adapter.
        self._clock = clock
        self._stop = stop
        self._last = clock()
        self._deadline = self._last
        self._policy: tuple[int, str] | None = None
        self._stopped = False
        self.running = False

    def _halt(self, reason: str) -> bool:
        was_running = self.running
        self.running = False
        self._stopped = True  # latch before callback, even if process termination fails
        if was_running:
            self._stop(reason)
        return False

    def check(self) -> bool:
        now = self._clock()
        if not math.isfinite(now) or now < self._last:
            return self._halt('clock_invalid')
        self._last = now
        if self.running and now >= self._deadline:
            return self._halt('lease_expired')
        return self.running

    def renew(self, *, revision: int, digest: str, valid_for: float, reconciled: bool) -> bool:
        self.check()  # late renewal cannot resurrect an expired execution
        if self._stopped:
            return False
        if not reconciled or revision < 1 or not digest or not math.isfinite(valid_for) or valid_for <= 0:
            return self._halt('authority_invalid')
        if self._policy is not None and self._policy != (revision, digest):
            return self._halt('policy_changed')
        self._policy = (revision, digest)
        self._deadline = self._last + min(300.0, valid_for)
        self.running = True
        return True
