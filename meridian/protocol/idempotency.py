"""Idempotency keys, because retries are ordinary now.

The 2026 revision removed stream resumability, so a client whose response stream
breaks re-issues the request with a new id and no way to know whether the first
attempt landed. Any tool with side effects therefore needs a way to say "this is
the same intent as before, do not do it twice".

The protocol supplies nothing for this. It is an ordinary tool argument, which
is why it works on every client and every transport with no negotiation.

The subtle parts are not the dictionary lookup:

  - the key alone is not the cache key. Two callers may generate the same key,
    and one caller may reuse a key with different arguments by mistake. Both
    are caught by fingerprinting the arguments alongside the key.
  - a concurrent duplicate must not do the work twice. The second arrival has
    to wait for the first rather than find an empty slot and proceed.
  - the record has to be written before the response is returned, or a crash in
    between leaves the work done and unrecorded.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import errors

DEFAULT_RETENTION_SECONDS = 24 * 3600


def fingerprint(arguments: dict) -> str:
    """A stable digest of the arguments a key was first used with."""
    payload = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


@dataclass
class Record:
    key: str
    principal: str | None
    fingerprint: str
    stored_at: float
    result: Any = None
    done: threading.Event = field(default_factory=threading.Event)


class IdempotencyStore:
    """Remembers what a key did, for as long as a client might retry it.

    In-memory here, and the same caveat applies as to the task store: behind a
    load balancer this must be shared, or a retry landing on another replica
    finds no record and does the work a second time.
    """

    def __init__(self, *, retention_seconds: float = DEFAULT_RETENTION_SECONDS,
                 clock: Callable[[], float] = time.time):
        self._records: dict[tuple[str | None, str], Record] = {}
        self._lock = threading.Lock()
        self.retention_seconds = retention_seconds
        self._clock = clock
        self.replays = 0

    def run(self, key: str, principal: str | None, arguments: dict,
            work: Callable[[], Any], *, wait_timeout: float = 30.0) -> Any:
        """Do the work once per key, and return the same answer to every retry."""
        if not key:
            raise errors.InvalidParams("idempotencyKey must be a non-empty string")

        digest = fingerprint(arguments)
        slot = (principal, key)
        now = self._clock()

        with self._lock:
            record = self._records.get(slot)
            if record is not None and now - record.stored_at > self.retention_seconds:
                del self._records[slot]
                record = None
            if record is None:
                record = Record(key=key, principal=principal, fingerprint=digest,
                                stored_at=now)
                self._records[slot] = record
                owner = True
            else:
                owner = False

        if owner:
            try:
                record.result = work()
            except BaseException:
                # A failure must not be remembered as a result, or the caller
                # can never retry it. Drop the record and let the next attempt
                # start clean.
                with self._lock:
                    self._records.pop(slot, None)
                record.done.set()
                raise
            record.done.set()
            return record.result

        # A replay. Reusing one key for two different requests is a client bug
        # worth surfacing loudly, because the alternative is returning one
        # operation's result as though it were another's.
        if record.fingerprint != digest:
            raise errors.InvalidParams(
                "idempotencyKey was already used with different arguments")

        if not record.done.wait(wait_timeout):
            raise errors.InternalError(
                "a request with this idempotencyKey is still in flight")
        self.replays += 1
        return record.result

    def sweep(self) -> int:
        now = self._clock()
        with self._lock:
            dead = [k for k, v in self._records.items()
                    if now - v.stored_at > self.retention_seconds]
            for k in dead:
                del self._records[k]
        return len(dead)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


IDEMPOTENCY_KEY_SCHEMA = {
    "type": "string",
    "minLength": 8,
    "maxLength": 128,
    "description": (
        "Client-generated unique id for this intent. Repeating a key within "
        "24 hours returns the original result rather than performing the "
        "action again. Generate a fresh key per intent; reuse it on retries."
    ),
}
