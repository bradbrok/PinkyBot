"""#637 tenant-grouping: the isolated-agent same-group messaging exception.

The #149 isolation boundary denies an isolated agent every cross-agent
``/agents/{other}/*`` surface. #637 carves out exactly one case — teammate
coordination: an isolated agent may reach a PEER it shares a group with, and
ONLY at that peer's inter-agent message-delivery endpoint. These tests pin the
pure policy predicate (`_isolated_peer_message_allowed`) so the exception can
never silently widen past "same-group peer, message endpoint only".
"""

from pinky_daemon.api import _isolated_peer_message_allowed

MSG = "/agents/chekov/message"


def test_shared_group_message_path_allowed():
    assert _isolated_peer_message_allowed(
        "POST", MSG, "chekov", ["support"], ["support"]
    ) is True


def test_multiple_groups_any_intersection_allows():
    assert (
        _isolated_peer_message_allowed(
            "POST", MSG, "chekov", ["a", "support", "b"], ["c", "support"]
        )
        is True
    )


def test_no_shared_group_denied():
    assert _isolated_peer_message_allowed(
        "POST", MSG, "chekov", ["support"], ["sales"]
    ) is False


def test_empty_or_missing_groups_denied():
    # a group each but no overlap, one side empty, both empty, both None
    for cg, pg in [([], ["support"]), (["support"], []), ([], []), (None, None)]:
        assert _isolated_peer_message_allowed("POST", MSG, "chekov", cg, pg) is False


def test_blank_or_malformed_group_values_never_authorize():
    for caller_groups, peer_groups in (
        ([""], [""]),
        (["   "], ["   "]),
        ("support", "sales"),
        (["support", {"bad": "shape"}], ["sales", {"bad": "shape"}]),
    ):
        assert _isolated_peer_message_allowed(
            "POST", MSG, "chekov", caller_groups, peer_groups
        ) is False


def test_non_post_methods_at_exact_message_path_denied():
    for method in ("GET", "DELETE", "PATCH", "OPTIONS"):
        assert _isolated_peer_message_allowed(
            method, MSG, "chekov", ["support"], ["support"]
        ) is False


def test_non_message_cross_agent_paths_denied_even_with_shared_group():
    # admin / config / autonomy / session / file / bare-agent surfaces all stay
    # denied — the exception is message-delivery only.
    for path in (
        "/agents/chekov/restart",
        "/agents/chekov/soul",
        "/agents/chekov/config",
        "/agents/chekov/update",
        "/autonomy/chekov/pause",
        "/agents/chekov/file",
        "/agents/chekov",
        "/agents/chekov/sessions/main/message",
    ):
        assert (
            _isolated_peer_message_allowed(
                "POST", path, "chekov", ["support"], ["support"]
            )
            is False
        ), path


def test_message_subpath_not_matched():
    # exact-match only — nothing nested under /message slips through.
    assert (
        _isolated_peer_message_allowed(
            "POST", MSG + "/evil", "chekov", ["support"], ["support"]
        )
        is False
    )


def test_target_must_match_path_segment():
    # target is the path-extracted agent; a mismatch between the named target
    # and the path segment must never match (belt-and-braces on the invariant).
    assert (
        _isolated_peer_message_allowed(
            "POST", "/agents/sasha/message", "chekov", ["support"], ["support"]
        )
        is False
    )
