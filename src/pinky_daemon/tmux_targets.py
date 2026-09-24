"""Exact-match ``-t`` targets for daemon tmux commands.

tmux resolves a bare ``-t NAME`` by exact session name first, then by unique
prefix, then by fnmatch pattern. When ``pinky-x`` is absent but ``pinky-x-old``
is running, a command aimed at ``pinky-x`` silently lands on ``pinky-x-old``:
it probes, captures, types into, resizes, renames or kills the wrong session.

A leading ``=`` restricts tmux to an exact session match, but only where the
string is parsed as a session:

* target-session commands (``has-session``, ``kill-session``,
  ``rename-session``) take ``=NAME``.
* target-window and target-pane commands (``send-keys``, ``capture-pane``,
  ``paste-buffer``, ``resize-window``, ``set-option``, ``display-message``, ...)
  read a colon-less string as a window or pane specifier. ``=NAME`` there
  either fails even when the session exists (pane commands) or still falls
  back to a session prefix match (window commands). They need ``=NAME:``: the
  ``=`` binds to the session part, and the empty window part selects that
  session's current window and its active pane.

Every tmux ``-t`` argument in the daemon is built by one of these helpers;
``tests/test_tmux_target_guard.py`` fails if a call site builds one without
them or uses the wrong kind for its command.
"""

from __future__ import annotations


def _addressable(session_name: str) -> str:
    """Return ``session_name`` if tmux can address it exactly, else raise.

    tmux stores ``.`` and ``:`` in a session name as ``_``, so a name holding
    either can never match exactly; a leading ``$`` is parsed as a session id.
    Refusing such names keeps a malformed name from silently addressing a
    different session.
    """
    if not isinstance(session_name, str) or not session_name:
        raise ValueError("tmux session name must be a non-empty string")
    if "." in session_name or ":" in session_name or session_name.startswith("$"):
        raise ValueError(f"tmux cannot address session name exactly: {session_name!r}")
    return session_name


def exact_session_target(session_name: str) -> str:
    """``-t`` value for a target-session command, matching only ``session_name``."""
    return "=" + _addressable(session_name)


def exact_pane_target(session_name: str) -> str:
    """``-t`` value for a target-window or target-pane command.

    Addresses the current window and active pane of exactly ``session_name``.
    """
    return "=" + _addressable(session_name) + ":"
