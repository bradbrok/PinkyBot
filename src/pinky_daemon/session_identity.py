"""Session identifiers and ownership evidence for filtering existing scopes."""

import logging
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from threading import Lock

_LOGGER = logging.getLogger(__name__)
_COLLISIONS_WARNED: set[str] = set()
_COLLISION_LOCK = Lock()


def _warn_collision(session_id: str, owners: Collection[str]) -> None:
    with _COLLISION_LOCK:
        if session_id in _COLLISIONS_WARNED:
            return
        _COLLISIONS_WARNED.add(session_id)
    _LOGGER.warning("Session ownership collision: id=%r agents=%s", session_id, ",".join(sorted(owners)))


def session_id_for_agent(agent_name: str, label: str = "main") -> str:
    """Keep the established external identifier format."""
    return f"{agent_name}-{label}"


def session_key_for_agent(agent_name: str, label: str) -> tuple[str, str]:
    """Keep the two components distinct for internal subscriptions."""
    return agent_name, label


def resolve_session_owner(
    session_id: str,
    registered_names: Iterable[str],
    *,
    live_owner: str = "",
    recorded_owner: str = "",
    allow_exact: bool = False,
) -> str:
    """Prefer explicit live and recorded owners before the longest name prefix.

    Exact bare names are accepted only for lifecycle-event attribution.
    An empty result supplies no reason to remove an existing candidate.
    """
    if live_owner:
        return live_owner
    if recorded_owner:
        return recorded_owner
    return max(
        (name for name in registered_names
         if session_id.startswith(name + "-") or (allow_exact and session_id == name)),
        key=len,
        default="",
    )


@dataclass(frozen=True)
class SessionOwnership:
    agent_name: str
    registered_names: tuple[str, ...]
    live_owners: dict[str, str]
    recorded_ids: frozenset[str]
    explicit_owners: dict[str, frozenset[str]] = field(default_factory=dict)
    family_names: frozenset[str] = frozenset()

    def keeps(self, session_id: str) -> bool:
        """Remove conflicting owners without widening the caller's candidates."""
        live_owner = self.live_owners.get(session_id, "")
        # Preserve the legacy authority outside registered prefix families.
        if live_owner and live_owner != self.agent_name and live_owner not in self.family_names:
            return False
        claimants = self.explicit_owners.get(session_id)
        if claimants:
            return claimants == {self.agent_name}
        owner = resolve_session_owner(
            session_id, self.registered_names,
            live_owner=self.live_owners.get(session_id, ""),
            recorded_owner=self.agent_name if session_id in self.recorded_ids else "",
        )
        return not owner or owner == self.agent_name

    @property
    def exclude_prefixes(self) -> tuple[str, ...]:
        return tuple(name + "-" for name in self.registered_names
                     if name.startswith(self.agent_name + "-"))

    @property
    def exclude_session_ids(self) -> tuple[str, ...]:
        excluded = {sid for sid, owner in self.live_owners.items() if owner != self.agent_name}
        excluded.update(sid for sid, owners in self.explicit_owners.items()
                        if owners != {self.agent_name})
        return tuple(excluded)

    @property
    def retain_session_ids(self) -> tuple[str, ...]:
        ids = self.recorded_ids | {
            sid for sid, owner in self.live_owners.items() if owner == self.agent_name
        } | {
            sid for sid, owners in self.explicit_owners.items() if owners == {self.agent_name}
        }
        return tuple(sid for sid in ids if self.keeps(sid))


def session_ownership(
    registry,
    agent_name: str,
    *,
    live_sessions: Collection = (),
    recorded_sessions: Collection | None = None,
    broker_sessions: Mapping[str, dict] | None = None,
) -> SessionOwnership:
    """Snapshot the available authorities, including retired registered names."""
    if recorded_sessions is None:
        recorded_sessions = registry.list_streaming_session_ids(agent_name)
    names = tuple(agent.name for agent in registry.list(include_retired=True))
    live_owners = {session.id: session.agent_name for session in live_sessions
                   if getattr(session, "agent_name", "")}
    recorded_ids = frozenset(entry["session_id"] for entry in recorded_sessions
                             if entry.get("session_id"))
    family = frozenset(name for name in names if name == agent_name
                       or name.startswith(agent_name + "-") or agent_name.startswith(name + "-"))
    # With no related registered name, retain the existing work and decisions.
    if not family - {agent_name}:
        return SessionOwnership(agent_name, names, live_owners, recorded_ids)

    owners: dict[str, set[str]] = {}
    for sid, owner in live_owners.items():
        if owner in family:
            owners.setdefault(sid, set()).add(owner)
    if broker_sessions is not None:
        for owner in family:
            # Copy each plain dictionary before iteration, without reading any
            # mutable session state or transport handle and without yielding.
            labels = broker_sessions.get(owner, {}).copy()
            for label in labels:
                owners.setdefault(session_id_for_agent(owner, label), set()).add(owner)
    for entry in registry.list_streaming_session_labels(family):
        owner = entry["agent_name"]
        owners.setdefault(session_id_for_agent(owner, entry["label"]), set()).add(owner)
    explicit = {sid: frozenset(claimants) for sid, claimants in owners.items()}
    for sid, claimants in explicit.items():
        if len(claimants) > 1:
            _warn_collision(sid, claimants)
    return SessionOwnership(agent_name, names, live_owners, recorded_ids, explicit, family)


class SessionIdScope(set[str]):
    """A resolved ID set with the same snapshot's SQL fallback exclusions."""

    def __init__(self, candidates: Iterable[str], ownership: SessionOwnership):
        super().__init__(sid for sid in candidates if ownership.keeps(sid))
        self.ownership = ownership
