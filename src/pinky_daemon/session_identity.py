"""Session identifiers and ownership evidence for filtering existing scopes."""

from collections.abc import Collection, Iterable
from dataclasses import dataclass


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

    def keeps(self, session_id: str) -> bool:
        """Remove conflicting owners without widening the caller's candidates."""
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
        return tuple(sid for sid, owner in self.live_owners.items()
                     if owner != self.agent_name)

    @property
    def retain_session_ids(self) -> tuple[str, ...]:
        ids = self.recorded_ids | {
            sid for sid, owner in self.live_owners.items() if owner == self.agent_name
        }
        return tuple(sid for sid in ids if self.keeps(sid))


def session_ownership(
    registry,
    agent_name: str,
    *,
    live_sessions: Collection = (),
    recorded_sessions: Collection | None = None,
) -> SessionOwnership:
    """Snapshot the available authorities, including retired registered names."""
    if recorded_sessions is None:
        recorded_sessions = registry.list_streaming_session_ids(agent_name)
    return SessionOwnership(
        agent_name,
        tuple(agent.name for agent in registry.list(include_retired=True)),
        {session.id: session.agent_name for session in live_sessions
         if getattr(session, "agent_name", "")},
        frozenset(entry["session_id"] for entry in recorded_sessions if entry.get("session_id")),
    )


class SessionIdScope(set[str]):
    """A resolved ID set with the same snapshot's SQL fallback exclusions."""

    def __init__(self, candidates: Iterable[str], ownership: SessionOwnership):
        super().__init__(sid for sid in candidates if ownership.keeps(sid))
        self.ownership = ownership
