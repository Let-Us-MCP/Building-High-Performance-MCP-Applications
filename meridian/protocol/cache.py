"""Client-side caching driven by `ttlMs` and `cacheScope`.

Six operations carry caching hints as of 2026-07-28:

    server/discover, tools/list, prompts/list,
    resources/list, resources/templates/list, resources/read

The rules are HTTP's rules with the serial numbers filed off. `ttlMs` is
`max-age`. `cacheScope` is `public` versus `private`. The parts worth getting
right are the parts people get wrong:

  * TTL is a *freshness hint*, not a polling interval. Checking freshness when
    you need the data is correct. Waking up every `ttlMs` to refetch data
    nobody asked for is a self-inflicted denial of service, and at scale it is
    a synchronised one.
  * `private` responses must be keyed by authorization context. Two users
    behind one gateway sharing a `private` cache entry is a data breach with
    good intentions.
  * MRTR retries are never cacheable. Their result depends on `inputResponses`,
    which is not part of the cache key, so caching one would serve one user's
    answer to another user's question.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

CACHEABLE_METHODS = frozenset({
    "server/discover",
    "tools/list",
    "prompts/list",
    "resources/list",
    "resources/templates/list",
    "resources/read",
})

# Params that participate in the cache key. Everything else (`_meta`,
# `progressToken`) varies per request without varying the answer.
KEY_PARAMS = ("uri", "cursor", "name")


@dataclass
class CacheEntry:
    value: dict
    stored_at: float
    ttl_ms: int
    scope: str
    server: str = ""
    method: str = ""

    def fresh_at(self, now: float) -> bool:
        return now < self.stored_at + (self.ttl_ms / 1000.0)

    def age_ms(self, now: float) -> float:
        return (now - self.stored_at) * 1000.0


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    stale: int = 0
    invalidations: int = 0
    stores: int = 0
    uncacheable: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses + self.stale
        return self.hits / total if total else 0.0

    def to_json(self) -> dict:
        return {
            "hits": self.hits, "misses": self.misses, "stale": self.stale,
            "invalidations": self.invalidations, "stores": self.stores,
            "uncacheable": self.uncacheable,
            "hitRate": round(self.hit_rate, 4),
        }


def cache_key(server: str, method: str, params: dict, auth_context: str) -> str:
    """Method plus the params that affect the answer, plus who is asking.

    `auth_context` is folded in unconditionally rather than only for `private`
    entries. Doing it the other way round means a single mislabelled response
    leaks across users, and the cost of being wrong is far higher than the cost
    of a few duplicate entries.
    """
    salient = {k: params[k] for k in KEY_PARAMS if k in params}
    blob = json.dumps(
        [server, method, salient, auth_context], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ResultCache:
    """A TTL cache that honours the protocol's hints rather than inventing its own."""

    def __init__(self, *, max_entries: int = 4096, clock=time.monotonic):
        self._entries: dict[str, CacheEntry] = {}
        self._lock = threading.RLock()
        self._max = max_entries
        self._clock = clock
        self.stats = CacheStats()

    # -- reads
    def get(self, server: str, method: str, params: dict,
            auth_context: str = "anon") -> dict | None:
        if method not in CACHEABLE_METHODS:
            self.stats.uncacheable += 1
            return None
        # A retry carrying MRTR fields is a different question with the same name.
        if "inputResponses" in params or "requestState" in params:
            self.stats.uncacheable += 1
            return None

        key = cache_key(server, method, params, auth_context)
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.stats.misses += 1
                return None
            if not entry.fresh_at(now):
                self.stats.stale += 1
                del self._entries[key]
                return None
            self.stats.hits += 1
            return entry.value

    # -- writes
    def put(self, server: str, method: str, params: dict, result: dict,
            auth_context: str = "anon") -> bool:
        if method not in CACHEABLE_METHODS:
            return False
        if "inputResponses" in params or "requestState" in params:
            return False
        if result.get("resultType", "complete") != "complete":
            return False  # interim results carry no hints and are never fresh

        ttl_ms = result.get("ttlMs")
        if not isinstance(ttl_ms, (int, float)):
            # Absent means "assume 0". Older servers land here.
            return False
        ttl_ms = int(ttl_ms)
        if ttl_ms <= 0:
            return False  # negative is ignored, zero means immediately stale

        scope = result.get("cacheScope", "private")
        if scope not in ("public", "private"):
            scope = "private"

        key = cache_key(server, method, params, auth_context)
        with self._lock:
            if len(self._entries) >= self._max:
                # Drop the oldest. A real client would use an LRU; the eviction
                # policy is not what this book is about.
                oldest = min(self._entries.items(), key=lambda kv: kv[1].stored_at)[0]
                del self._entries[oldest]
            self._entries[key] = CacheEntry(
                result, self._clock(), ttl_ms, scope, server=server, method=method
            )
            self.stats.stores += 1
        return True

    # -- invalidation
    def invalidate_method(self, server: str, method: str) -> int:
        """Drop every entry for one method on one server.

        This is what `notifications/tools/list_changed` triggers. A change
        notification is an immediate invalidation signal, and it overrides a TTL
        that has not expired yet. The two mechanisms are complementary: the TTL
        stops you refetching between changes, the notification stops you serving
        stale data after one.

        Paginated lists live under many keys (one per cursor), so this sweeps
        by method rather than by key.
        """
        with self._lock:
            doomed = [
                k for k, e in self._entries.items()
                if e.server == server and e.method == method
            ]
        return self._invalidate_keys(doomed)

    def invalidate_uri(self, server: str, uri: str) -> int:
        """Drop cached reads of one resource, for `notifications/resources/updated`."""
        with self._lock:
            doomed = [
                k for k, e in self._entries.items()
                if e.server == server
                and e.method == "resources/read"
                and e.value.get("contents", [{}])[0].get("uri") == uri
            ]
        return self._invalidate_keys(doomed)

    def _invalidate_keys(self, keys: list[str]) -> int:
        removed = 0
        with self._lock:
            for key in keys:
                if self._entries.pop(key, None) is not None:
                    removed += 1
        self.stats.invalidations += removed
        return removed

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
