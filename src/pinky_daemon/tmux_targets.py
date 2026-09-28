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

import re

# Session names are a fixed prefix plus an agent name, and agent names are
# validated to lowercase ASCII letters, digits, ``_`` and ``-``. tmux matches a
# name made only of these characters exactly.
_ADDRESSABLE_NAME = re.compile(r"[a-z0-9_-]+")


def _addressable(session_name: str) -> str:
    """Return ``session_name`` if tmux can address it exactly, else raise.

    Only names built from the characters above are accepted. Anything else
    can address a different session or none: tmux stores ``.`` and ``:`` as
    ``_`` and control characters in escaped form, a leading ``$`` names a
    session id, and an argument ending in ``;`` is split off as a command
    separator (``=NAME;`` addresses ``NAME``).
    """
    if not isinstance(session_name, str) or not _ADDRESSABLE_NAME.fullmatch(session_name):
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


def text_argument(text: str) -> str:
    r"""``text`` as a tmux argument that tmux passes on unchanged.

    tmux splits an argument ending in ``;`` off as a command separator (the
    ``;`` is dropped) and turns a trailing ``\;`` into ``;``. Escaping the
    final ``;`` as ``\;`` makes tmux restore exactly ``text``. Pair it with
    ``--`` so text starting with ``-`` is not read as flags either.
    """
    if text.endswith(";"):
        return text[:-1] + "\\;"
    return text
