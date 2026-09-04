"""Client transaction timers (RFC 3261 §17.1).

Small on purpose. sip-tt needs retransmission for two reasons and neither is
"be a good citizen": a DUT that only answers the second INVITE has a bug worth
naming, and several test purposes are *about* the timers — does the device
absorb a retransmitted request rather than treating it as new, does it give up
at the right time.

The deliberate difference from a production stack is that nothing here is
hidden. A test can read `attempts`, `elapsed` and every retransmission time,
because "how many times did we have to ask" is the measurement.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

T1 = 0.5      # RFC 3261 §17.1.1.1 — the RTT estimate everything derives from
T2 = 4.0      # retransmission interval cap for non-INVITE
TIMER_B = 64 * T1   # INVITE transaction timeout: 32 s
TIMER_F = 64 * T1   # non-INVITE transaction timeout: 32 s


@dataclass
class ClientTransaction:
    """Tracks one request's retransmission ladder."""

    method: str
    branch: str
    request: str
    dest: tuple[str, int]

    started: float = field(default_factory=time.monotonic)
    attempts: int = 1
    interval: float = T1
    next_retransmit: float = 0.0
    sent_at: list[float] = field(default_factory=list)

    provisional: bool = False    # a 1xx has arrived; Timer B is cancelled
    final_status: int = 0
    done: bool = False

    def __post_init__(self) -> None:
        self.next_retransmit = self.started + T1
        self.sent_at.append(self.started)

    @property
    def is_invite(self) -> bool:
        return self.method == "INVITE"

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def due(self, now: float) -> bool:
        """Whether a retransmission is owed right now."""
        if self.done or self.provisional:
            return False
        return now >= self.next_retransmit

    def on_retransmit(self, now: float) -> None:
        self.attempts += 1
        self.sent_at.append(now)
        # INVITE's Timer A is uncapped in the RFC; non-INVITE's Timer E caps at
        # T2. Both are capped here, because an uncapped ladder inside a 32 s
        # budget only changes how many packets a failing test sends.
        self.interval = min(self.interval * 2, T2)
        self.next_retransmit = now + self.interval

    def timed_out(self, now: float) -> bool:
        """Timer B / F. Cancelled by a provisional response, per §17.1.1.2."""
        if self.done or self.provisional:
            return False
        return (now - self.started) >= (TIMER_B if self.is_invite else TIMER_F)

    def on_response(self, status: int) -> None:
        if status < 200:
            self.provisional = True
        else:
            self.final_status = status
            self.done = True
