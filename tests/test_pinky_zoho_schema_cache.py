from __future__ import annotations

from pinky_zoho.schema_cache import SchemaCache


def test_schema_cache_uses_value_until_ttl_expires() -> None:
    now = 1000.0
    calls: list[str] = []

    def time_fn() -> float:
        return now

    def fetch(module: str) -> dict:
        calls.append(module)
        return {"module": module, "version": len(calls)}

    cache = SchemaCache(ttl_seconds=60, time_fn=time_fn)

    first = cache.get("Events", fetch)
    second = cache.get("Events", fetch)
    now = 1061.0
    third = cache.get("Events", fetch)

    assert first == {"module": "Events", "version": 1}
    assert second is first
    assert third == {"module": "Events", "version": 2}
    assert calls == ["Events", "Events"]


def test_schema_cache_refresh_and_clear() -> None:
    counter = 0

    def fetch(module: str) -> dict:
        nonlocal counter
        counter += 1
        return {"module": module, "counter": counter}

    cache = SchemaCache()

    assert cache.get("Tasks", fetch)["counter"] == 1
    assert cache.refresh("Tasks", fetch)["counter"] == 2
    cache.clear("Tasks")
    assert cache.get("Tasks", fetch)["counter"] == 3
    cache.clear()
    assert cache.get("Tasks", fetch)["counter"] == 4
