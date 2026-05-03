"""Per-domain FastAPI routers for the Pinky API.

Each module in this package defines an `APIRouter` plus a
`set_dependencies(...)` function that `pinky_daemon.api.create_api()`
calls before `app.include_router(...)`. This pattern mirrors the
existing `pinky_daemon.migration.routes` and `pinky_daemon.voice_routes`
modules and lets us extract route blocks out of the monolithic api.py
without changing endpoint paths or behavior.
"""
