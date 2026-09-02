"""In-memory per-IP token bucket (plan 03.4 ruling 12).

Per-process state, accepted deliberately: N uvicorn workers multiply the
effective limit by N. Real distributed limiting needs shared storage (Redis),
which decision #36 rejected for now; revisit if the process count grows."""

import math
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.deps import get_db
from app.core.errors import err


@dataclass
class _Bucket:
    tokens: float
    updated: float


_buckets: dict[tuple[str, str], _Bucket] = {}
# How many buckets each scope holds. Derivable from `_buckets` — and derived on
# every request is exactly what it may not be: the sweep gate below has to answer
# "is any ONE scope over its cap", and scanning the dict for that would put an
# O(n) walk on the event loop for every rate-limited request. Kept in step by
# `consume` (the only creator) and `_drop` (the only remover), so the two cannot
# disagree; `test_the_scope_counts_stay_in_step_with_the_buckets` pins it.
_scope_counts: dict[str, int] = {}
_last_sweep: float = 0.0

# Eviction (3.11a t5, review I1/I3). Until the public QR check, every scope here
# was reached only by a caller who had first found a login form or been sent a
# webhook secret. That route is advertised on printed documents and needs no
# credentials at all, so anybody on the internet can mint one dict entry per
# source address — unbounded over IPv6, and never freed. Two limits, additive:
#
#   * a bucket refills at `per_minute/60` tokens a second and is capped at
#     `per_minute`, so after 60 idle seconds it is FULL whatever its setting is.
#     Deleting a full bucket is exactly equivalent to keeping it — recreating it
#     yields the same state — which makes the TTL sweep free of any semantics.
#   * `MAX_BUCKETS` is the backstop for a burst wide enough to outrun the sweep
#     window: the least recently touched entries are dropped down to the cap.
#     That one IS lossy — a dropped bucket's holder gets a fresh budget — but a
#     bounded, briefly-generous limiter beats an unbounded one, and it takes
#     `MAX_BUCKETS` distinct addresses within a second to reach.
#
# **`MAX_BUCKETS` is PER SCOPE** (ruling T5-e), and that is a security property,
# not a tuning choice. `_buckets` is one dict for every scope, so a cap enforced
# across the whole dict let a flood on the open QR route evict a `("login", ip)`
# bucket sitting at zero tokens — an attacker who had burned their login budget
# could hand it back to themselves from a /64, and 20,000 addresses fits inside a
# single sweep window. Before the cap existed nothing could be evicted at all, so
# that primitive arrived with it. Exempting the "non-public" scopes instead would
# not do: `/auth/login` is just as reachable by a stranger as the QR page, and
# leaving it uncapped only restores the leak on the scope that matters most. The
# DB-backed per-account `login_max_attempts` lockout is a different control on a
# different key and is untouched either way.
#
# **What this does NOT close.** A flood against `/auth/login` ITSELF still evicts
# a victim's burned login bucket — same scope, so the cap pass reaches it. The
# attacker now has to spend `MAX_BUCKETS` distinct addresses on the guarded route
# instead of the free, unauthenticated QR page, and the per-account
# `login_max_attempts` lockout in the database still applies to the account they
# are actually after. That is a real reduction in reach, not a closure, and the
# only thing that would close it is per-scope state an attacker cannot enlarge —
# shared storage (Redis), which decision #36 has deferred.
BUCKET_IDLE_SECONDS = 60.0
MAX_BUCKETS = 20_000
SWEEP_EVERY_SECONDS = 1.0


def reset() -> None:
    """Tests only: forget every bucket."""
    global _last_sweep
    _buckets.clear()
    _scope_counts.clear()
    _last_sweep = 0.0


def _drop(key: tuple[str, str]) -> None:
    """The ONLY place a bucket is removed, so `_scope_counts` cannot drift from
    `_buckets`. A scope that falls to zero loses its entry rather than keeping a
    `0`, which is what lets `max(...)` below read the live maximum."""
    del _buckets[key]
    remaining = _scope_counts[key[0]] - 1
    if remaining:
        _scope_counts[key[0]] = remaining
    else:
        del _scope_counts[key[0]]


def _sweep(now: float, keep: tuple[str, str]) -> None:
    """Drop refilled buckets, then trim each SCOPE to `MAX_BUCKETS`. Cheap and
    amortised: at most once a `SWEEP_EVERY_SECONDS`, or immediately whenever some
    scope is already over its cap — the one case where waiting is the wrong answer.

    The TTL pass stays global, and safely so: a bucket idle for `BUCKET_IDLE_
    SECONDS` has refilled to full, and deleting a full bucket is exactly
    equivalent to keeping it. The CAP pass is the lossy one — a dropped bucket's
    holder gets a fresh budget — which is why it may only ever reach inside the
    scope that overflowed. Trimming across scopes made the open QR route a reset
    button for `/auth/login` (ruling T5-e, the comment above `MAX_BUCKETS`).

    **The gate asks about one scope, not about the dict.** With per-scope caps the
    dict may legally sit at k scopes x `MAX_BUCKETS`, so the old whole-dict test
    (`len(_buckets) > MAX_BUCKETS`) would be permanently true in the ordinary
    post-flood steady state — the QR scope at its cap plus a single login bucket —
    and every request would then run this whole body, freeing nothing: an O(n) TTL
    walk plus an O(n) regroup, on the event loop, indefinitely (review, Important
    2). `max(_scope_counts.values())` is O(number of scopes), a handful, and it is
    false exactly when there is nothing for the cap pass to do.

    `keep` is the caller's OWN bucket, exempt from both passes. It is the newest
    entry and a zero-second-old one, so under the production values neither pass
    could reach it anyway — but "the limiter cannot evict the budget it is in the
    middle of charging" is a property worth holding structurally rather than by
    arithmetic that a smaller configured TTL would quietly break.
    """
    global _last_sweep
    if now - _last_sweep < SWEEP_EVERY_SECONDS and not _some_scope_over_cap():
        return
    _last_sweep = now
    for key in [
        k for k, b in _buckets.items() if k != keep and now - b.updated >= BUCKET_IDLE_SECONDS
    ]:
        _drop(key)
    # The regroup is the expensive half and buys nothing when the TTL pass has
    # already brought every scope back under its cap — the common case by far.
    if not _some_scope_over_cap():
        return
    by_scope: dict[str, list[tuple[str, str]]] = {}
    for key in _buckets:
        by_scope.setdefault(key[0], []).append(key)
    for keys in by_scope.values():
        if len(keys) <= MAX_BUCKETS:
            continue
        stale = [k for k in sorted(keys, key=lambda k: _buckets[k].updated) if k != keep]
        for key in stale[: len(keys) - MAX_BUCKETS]:
            _drop(key)


def _some_scope_over_cap() -> bool:
    """Is any single scope above `MAX_BUCKETS`? O(scopes), not O(buckets)."""
    return max(_scope_counts.values(), default=0) > MAX_BUCKETS


async def consume(request: Request, db: AsyncSession, *, scope: str, setting_key: str) -> None:
    """Take one token from `(scope, client ip)`, or raise `ERR-SYS-006` (429).

    Callable directly, not only through the dependency below, because a route
    whose budget depends on the REQUEST cannot pick its scope at import time:
    the public permit check charges the guessable `?series=&number=` path to a
    different bucket than the scanned `?qr=` one (review I1), and a dependency
    would have to re-declare and re-parse the query parameters to know which.
    """
    per_minute = await settings_store.get_int(db, setting_key)
    per_minute = max(1, per_minute)  # never divide by zero, whatever the setting holds
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _buckets.get((scope, ip))
    if bucket is None:
        bucket = _Bucket(tokens=float(per_minute), updated=now)
        _buckets[(scope, ip)] = bucket
        # The only place a bucket is created, matching `_drop`'s sole removal.
        _scope_counts[scope] = _scope_counts.get(scope, 0) + 1
    else:
        bucket.tokens = min(
            float(per_minute), bucket.tokens + (now - bucket.updated) * per_minute / 60.0
        )
        bucket.updated = now
    # After this request's own bucket exists, never before: sweeping first would
    # trim to the cap and then add one more, leaving `MAX_BUCKETS + 1` behind
    # every call. It is passed as `keep` so it cannot be swept out from under the
    # charge about to be made against it.
    _sweep(now, (scope, ip))
    if bucket.tokens < 1.0:
        retry = math.ceil((1.0 - bucket.tokens) * 60.0 / per_minute)
        raise err("ERR-SYS-006", details={"retry_after_seconds": retry})
    bucket.tokens -= 1.0


def rate_limit(scope: str, setting_key: str):
    """Dependency factory: at most `setting_key` requests per minute per client IP."""

    async def dep(request: Request, db: Annotated[AsyncSession, Depends(get_db)]) -> None:
        await consume(request, db, scope=scope, setting_key=setting_key)

    return dep
