"""Audit every registered FastAPI route against the auth allowlists.

Walks ``app.routes`` from a real ``create_api`` instance (post-prefix-resolution)
and classifies each route as PROTECTED-API / PROTECTED-HTML / PUBLIC-exact /
PUBLIC-prefix / UNCLASSIFIED.

UNCLASSIFIED is the deny-by-default gap (#506): a path that is neither public
nor under a protected prefix falls through the auth middleware to the route.
Under ``PINKY_AUTH_DENY_DEFAULT=enforce`` such a path is 401'd, so every route
must be classified. This script is the basis for the regression test
(``tests/test_route_auth_coverage.py``).

The classification sets are read from ``app.state.auth_route_sets`` — the EXACT
tuples the auth middleware uses — so this audit never drifts from the gate.

Usage:
    python3 scripts/audit_route_auth_coverage.py
Exit status is non-zero if any route is UNCLASSIFIED.

Why app.routes (not source-grep): routers under ``APIRouter(prefix="/foo")``
register decorator strings like ``@router.get("/bar")`` which become ``/foo/bar``
at runtime. Source-grep misses that. ``app.routes`` is ground truth.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections import defaultdict


def _kind(route) -> str:
    """Map a concrete route object to the audit's kind label."""
    from fastapi.routing import APIRoute
    from starlette.routing import Mount, WebSocketRoute

    if isinstance(route, WebSocketRoute):
        return "WS"
    if isinstance(route, (APIRoute, Mount)):
        return "MOUNT" if isinstance(route, Mount) else "HTTP"
    return "HTTP"


def _included_children(router) -> list[tuple[str, str]] | None:
    """Expand a FastAPI ``_IncludedRouter`` into (kind, effective path) pairs.

    Returns ``None`` when ``router`` is not that kind of wrapper, so the caller
    can fall through to the other strategies.
    """
    candidates = getattr(router, "effective_candidates", None)
    if not callable(candidates):
        return None

    collected: list[tuple[str, str]] = []
    for ctx in candidates():
        nested = _included_children(ctx)
        if nested is not None:
            # A router included inside another router.
            collected.extend(nested)
            continue
        original = getattr(ctx, "original_route", None)
        path = getattr(ctx, "path", None)
        if path is None:
            raise RuntimeError(
                f"included route {type(ctx).__name__!r} has no effective .path — "
                "extend collect() rather than letting it go unaudited"
            )
        if _is_mount(original):
            collected.append(("MOUNT", path.rstrip("/") + "/"))
        else:
            collected.append((_kind(original), path))
    return collected


def _is_mount(route) -> bool:
    from starlette.routing import Mount

    return isinstance(route, Mount)


def _walk(container) -> list[tuple[str, str]]:
    """Collect (kind, path) for every route reachable from ``container.routes``."""
    from fastapi.routing import APIRoute
    from starlette.routing import Mount, Route, WebSocketRoute

    routes: list[tuple[str, str]] = []
    for r in container.routes:
        if isinstance(r, APIRoute):
            routes.append(("HTTP", r.path))
        elif isinstance(r, WebSocketRoute):
            routes.append(("WS", r.path))
        elif isinstance(r, Mount):
            # A Mount serves an entire subtree (e.g. /assets, /static). The auth
            # middleware sees the original path BEFORE routing, so the mount's
            # prefix must be classified too — a future sub-app mounted at an
            # unclassified prefix would otherwise be reachable while the
            # zero-unclassified gate stayed green. Represent the mount by its
            # subtree prefix (trailing slash) so it matches the public/protected
            # prefix tuples the middleware uses.
            routes.append(("MOUNT", r.path.rstrip("/") + "/"))
        elif isinstance(r, Route):
            # Plain Starlette routes — NOT APIRoutes. FastAPI registers /docs,
            # /redoc, /openapi.json and /docs/oauth2-redirect this way. Skipping
            # them made this audit BLIND (#510): they reach the app through the
            # same gate as everything else, and all four were unclassified while
            # the audit reported zero gaps. Anything callable must be collected.
            routes.append(("HTTP", r.path))
        else:
            # Never drop a route silently — an unrecognised route type is the
            # exact failure mode #510 exposed. Surface it instead.
            path = getattr(r, "path", None)
            if path is None:
                # FastAPI >= 0.141 wraps an included router in an _IncludedRouter:
                # no .path and no .routes of its own, but effective_candidates()
                # yields the child routes with their prefix already applied. Those
                # children are real, reachable paths — descend instead of failing,
                # so the audit stays exhaustive across dependency versions.
                nested = _included_children(r)
                if nested is not None:
                    routes.extend(nested)
                    continue
                # Older layouts expose the children directly.
                if getattr(r, "routes", None) is not None:
                    routes.extend(_walk(r))
                    continue
                raise RuntimeError(
                    f"uncollectable route {type(r).__name__!r} has no .path — "
                    "extend collect() rather than letting it go unaudited"
                )
            routes.append((type(r).__name__.upper()[:5], path))
    return routes


def collect(app) -> tuple[list[tuple[str, str]], dict]:
    """Return ([(kind, path), ...], auth_route_sets) for ``app``."""
    return _walk(app), app.state.auth_route_sets


def classifier(sets: dict):
    """Build a classify(path)->label fn from app.state.auth_route_sets."""
    public_exact = sets["public_exact"]
    public_prefixes = tuple(sets["public_prefixes"])
    protected_html = sets["protected_html"]
    protected_api = tuple(sets["protected_api_prefixes"])

    def classify(path: str) -> str:
        if path in public_exact:
            return "PUBLIC-exact"
        if path.startswith(public_prefixes):
            return "PUBLIC-prefix"
        if path in protected_html:
            return "PROTECTED-HTML"
        if path.startswith(protected_api):
            return "PROTECTED-API"
        return "UNCLASSIFIED"

    return classify


def build_app():
    """Instantiate a real create_api app against a throwaway DB."""
    os.environ.setdefault(
        "PINKY_SESSION_SECRET",
        "test-session-secret-do-not-use-in-prod-32bytes-min",
    )
    if "src" not in sys.path:
        sys.path.insert(0, "src")
    from pinky_daemon.api import create_api

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    return create_api(max_sessions=10, default_working_dir="/tmp", db_path=db_path)


def audit() -> list[tuple[str, str]]:
    """Return the sorted list of UNCLASSIFIED (kind, path) routes."""
    app = build_app()
    routes, sets = collect(app)
    classify = classifier(sets)
    unclass = [(k, p) for (k, p) in routes if classify(p) == "UNCLASSIFIED"]
    return sorted(set(unclass), key=lambda x: x[1])


def main() -> int:
    app = build_app()
    routes, sets = collect(app)
    classify = classifier(sets)

    by_class: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for kind, path in routes:
        by_class[classify(path)].append((kind, path))

    print(f"Total callable routes: {len(routes)}")
    print(f"Classes: {dict((k, len(v)) for k, v in by_class.items())}")

    unclass = sorted(set(by_class["UNCLASSIFIED"]), key=lambda x: x[1])
    print(f"\nUNCLASSIFIED ({len(unclass)}):")
    by_seg: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for kind, p in unclass:
        seg = "/" + p.lstrip("/").split("/", 1)[0]
        by_seg[seg].append((kind, p))
    for seg in sorted(by_seg):
        print(f"\n  {seg}/  ({len(by_seg[seg])} routes)")
        for kind, p in sorted(set(by_seg[seg])):
            print(f"    [{kind:5s}]  {p}")

    return 0 if not unclass else 1


if __name__ == "__main__":
    raise SystemExit(main())
