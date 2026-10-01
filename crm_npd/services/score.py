from __future__ import annotations
from typing import Any, Iterable
from ..config import CACHE_TTL_SECONDS
from .cache import TTLCache

_score_cache = TTLCache()


def _user_signature(user: dict[str, Any] | None) -> tuple:
    if not user:
        return ("anon", None, None)
    return (
        (user.get("role") or "").strip().lower(),
        user.get("id"),
        user.get("vendor_codigo") or user.get("vendor_id"),
    )


def _scope_signature(customer_ids_scope: Iterable[int] | None) -> tuple:
    if not customer_ids_scope:
        return ()
    return tuple(sorted(int(x) for x in customer_ids_scope if x))


def build_identity_score_map_cached(legacy_module, conn, user=None, customer_ids_scope=None):
    db_version = getattr(legacy_module, '_crm_cache_db_version', '0')
    key = ('score_map', _user_signature(user), _scope_signature(customer_ids_scope))
    cached = _score_cache.get(key, version=db_version)
    if cached is not None:
        return cached
    result = legacy_module._original_build_identity_score_map(conn, user=user, customer_ids_scope=customer_ids_scope)
    _score_cache.set(key, result, ttl_seconds=CACHE_TTL_SECONDS, version=db_version)
    return result


def clear_score_cache() -> None:
    _score_cache.clear()
