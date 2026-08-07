"""Buzz outbound registration gate used during daemon startup."""

from __future__ import annotations

import inspect
import sys

from pinky_outreach.buzz_dependency import missing_buzz_dependencies


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


async def register_buzz_outbound_platforms(registry, owner_notify) -> dict:
    """Refuse enabled Buzz identities loudly when runtime deps are missing."""
    identities = registry.list_buzz_identities(enabled_only=True)
    if not identities:
        return {"registered": 0, "refused": 0, "missing_dependencies": []}
    missing = missing_buzz_dependencies()
    if not missing:
        for identity in identities:
            registry.mark_buzz_dependency_ready(identity["agent"])
        return {"registered": len(identities), "refused": 0, "missing_dependencies": []}

    names = ", ".join(missing)
    for identity in identities:
        agent = identity["agent"]
        registry.mark_buzz_dependency_refused(agent, f"missing:{names}")
        message = (
            f"BUZZ PLATFORM REFUSED for {agent}: required daemon dependencies "
            f"are missing ({names}). Run the normal dependency heal/update; "
            "the identity remains encrypted and will re-register after restart."
        )
        delivered = False
        try:
            result = owner_notify(agent, message)
            delivered = bool(await result) if inspect.isawaitable(result) else bool(result)
        except Exception as exc:  # noqa: BLE001 — refusal must survive alert failure
            _log(f"buzz-startup: owner-notify failed for {agent} ({type(exc).__name__})")
        if not delivered:
            _log(f"buzz-startup: OWNER_NOTIFY_FAILED for refused identity {agent}")
    return {
        "registered": 0,
        "refused": len(identities),
        "missing_dependencies": list(missing),
    }


__all__ = ["register_buzz_outbound_platforms"]
