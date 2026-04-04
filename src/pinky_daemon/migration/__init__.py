"""OpenClaw → PinkyBot migration module.

Provides a three-step migration pipeline:
    1. parse  — unzip + read OpenClaw workspace files (no Claude, no DB)
    2. preview — Claude-assisted transformation: soul split, schedule parsing,
                  memory classification, directive splitting
    3. apply  — create agent in DB, spawn background memory import task

Public API:
    from pinky_daemon.migration.routes import router as migration_router
"""
