"""When to restart a stream that ended because the TV went to standby or off (no Home Assistant import).

The manager calls these methods on the event loop only. Every method that changes `enabled` or
`pending` returns whether the stored state changed, so the manager saves only then.
"""

from __future__ import annotations

import math

RESUME_REASONS = frozenset({"tv_standby", "tv_off"})
MIN_INTERVAL_S = 10.0  # at most one automatic start per this many seconds (a TV flapping while it boots)


class AutoResume:
    """Remembers whether a stream is waiting for the TV to come back and decides when to start it."""

    def __init__(self, enabled: bool = True, pending: bool = False, min_interval_s: float = MIN_INTERVAL_S):
        self.enabled = bool(enabled)
        self.pending = bool(pending) and self.enabled
        self.min_interval_s = min_interval_s
        self._last_attempt = -math.inf

    def _set_pending(self, pending: bool) -> bool:
        changed = pending != self.pending
        self.pending = pending
        return changed

    def stream_started(self) -> bool:
        return self._set_pending(False)

    def stream_ended(self, reason: str | None) -> bool:
        """Pending only if the option is on and the TV ended the stream ("user", "error", None do not)."""
        return self._set_pending(self.enabled and reason in RESUME_REASONS)

    def user_stopped(self) -> bool:
        return self._set_pending(False)

    def set_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        changed = enabled != self.enabled
        self.enabled = enabled
        if not enabled:
            changed = self._set_pending(False) or changed
        return changed

    def should_start(self, reader_state: str, running: bool, now: float) -> bool:
        """True if a waiting stream should start now; remembers `now` as the last attempt then."""
        if not (self.enabled and self.pending) or running or reader_state != "ok":
            return False
        if now - self._last_attempt < self.min_interval_s:
            return False
        self._last_attempt = now
        return True
