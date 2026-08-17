"""A tamper-evident audit log.

"Append-only" is easy to say and, in a plain list or an ordinary database table,
untrue: anybody who can write can also edit, and an attacker who reaches your
storage will edit the record of what they did before they leave.

Hash chaining makes that detectable. Each entry carries a digest computed over
its own contents and the previous entry's digest, so the log is a chain. Change
any earlier entry and every digest after it stops matching, and `verify` reports
the first index where the chain broke.

What this does and does not buy:

  detects   editing an entry, deleting an entry from the middle, reordering
  does not  an attacker who rewrites the whole chain from the edit onward

Closing that last gap needs the head digest published somewhere the attacker
does not control: a second store, a log-shipping pipeline, or a printout on a
wall. The chain is what makes a single published digest cover every entry
beneath it, which is the property that makes publishing cheap enough to do.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Callable

GENESIS = "0" * 64


def digest(entry: dict, previous: str) -> str:
    """Hash one entry together with its predecessor's digest.

    Serialised with sorted keys, because two encoders that disagree on field
    order would produce different digests for identical entries and the chain
    would fail to verify for a reason that has nothing to do with tampering.
    """
    body = json.dumps(entry, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(f"{previous}{body}".encode()).hexdigest()


class AuditChain:
    """An append-only log whose entries are linked by hash."""

    def __init__(self, *, clock: Callable[[], float] = time.time):
        self._entries: list[dict] = []
        self._lock = threading.Lock()
        self._clock = clock

    @property
    def head(self) -> str:
        """The digest covering every entry so far. Publish this."""
        with self._lock:
            return self._entries[-1]["digest"] if self._entries else GENESIS

    def append(self, entry: dict) -> dict:
        with self._lock:
            previous = self._entries[-1]["digest"] if self._entries else GENESIS
            record = dict(entry)
            record.setdefault("at", self._clock())
            record["previous"] = previous
            record["digest"] = digest(
                {k: v for k, v in record.items() if k != "digest"}, previous)
            self._entries.append(record)
            return record

    def verify(self) -> tuple[bool, int | None]:
        """Recompute the chain. Returns (ok, index of the first bad entry)."""
        previous = GENESIS
        with self._lock:
            entries = list(self._entries)
        for i, record in enumerate(entries):
            expected = digest(
                {k: v for k, v in record.items() if k != "digest"}, previous)
            if record.get("previous") != previous or record["digest"] != expected:
                return False, i
            previous = record["digest"]
        return True, None

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __getitem__(self, item: Any):
        with self._lock:
            return self._entries[item]

    def tail(self, limit: int) -> list[dict]:
        with self._lock:
            return list(self._entries[-limit:])
