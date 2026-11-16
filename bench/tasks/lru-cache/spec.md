Implement `LRUCache` in cache.py (standard library only):

- `LRUCache(capacity)`: `capacity` must be an int >= 1, else raise `ValueError`.
- `get(key, default=None)`: the value, or `default` if absent. A hit makes the key the most
  recently used.
- `put(key, value)`: insert or update; either way the key becomes the most recently used. When
  inserting into a full cache, first evict the least recently used key.
- `len(cache)` is the number of keys; `key in cache` tests presence without changing recency.

Write test_cache.py covering eviction order, updates, and the ValueError.
