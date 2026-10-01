from __future__ import annotations
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any, Hashable


@dataclass
class CacheItem:
    value: Any
    expires_at: float
    version: str


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[Hashable, CacheItem] = {}
        self._lock = RLock()

    def get(self, key: Hashable, *, version: str) -> Any | None:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            if item.expires_at < now or item.version != version:
                self._data.pop(key, None)
                return None
            return item.value

    def set(self, key: Hashable, value: Any, *, ttl_seconds: int, version: str) -> None:
        with self._lock:
            self._data[key] = CacheItem(value=value, expires_at=time.time() + ttl_seconds, version=version)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
