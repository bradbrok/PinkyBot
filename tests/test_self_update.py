"""Tests for pinky_daemon.self_update (deploy-target resolve + verify)."""

from __future__ import annotations

import subprocess

import pytest

from pinky_daemon.self_update import (
    commit_exists,
    github_tag_commit,
    latest_release_tag,
    local_tag_commit,
    origin_owner_repo,
    resolve_and_verify,
    tag_exists,
    verify_published_release,
)

OWNER_REPO = "bradbrok/PinkyBot"
TAG = "26.06.109"


@pytest.fixture
def repo(tmp_path):
    """A real git repo: one commit, https origin, no tags yet."""
    d = tmp_path / "repo"
    d.mkdir()

    def g(*args):
        return subprocess.check_output(
            ["git", *args], cwd=d, stderr=subprocess.DEVNULL
        ).decode().strip()

    g("init", "-q")
    g("config", "user.email", "t@t.test")
    g("config", "user.name", "t")
    g("config", "commit.gpgsign", "false")
    g("config", "tag.gpgsign", "false")
    (d / "f.txt").write_text("hi")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    g("remote", "add", "origin", f"https://github.com/{OWNER_REPO}.git")
    return d, g


def _http(routes):
    """Build an injectable http_get from {url_substring: (status, json)}."""
    def http_get(url):
        for key, val in routes.items():
            if key in url:
                return val
        return (0, None)  # unreachable
    return http_get


def _published_routes(repo_dir, g, tag=TAG, *, draft=False, prerelease=False):
    """Routes for a healthy published release whose commit matches local."""
    tag_obj = g("rev-parse", tag)  # annotated tag object sha
    head = g("rev-parse", "HEAD")
    return {
        f"/releases/tags/{tag}": (200, {"draft": draft, "prerelease": prerelease}),
        f"/git/ref/tags/{tag}": (200, {"object": {"type": "tag", "sha": tag_obj}}),
        f"/git/tags/{tag_obj}": (200, {"object": {"sha": head}}),
    }


# -- git helpers -------------------------------------------------------------


def test_latest_release_tag_picks_newest_calver(repo):
    d, g = repo
    g("tag", "-a", "26.05.099", "-m", "r")
    g("tag", "-a", "26.06.109", "-m", "r")
    g("tag", "-a", "not-a-release", "-m", "r")  # ignored — not CalVer
    assert latest_release_tag(str(d)) == "26.06.109"


def test_latest_release_tag_none_when_no_tags(repo):
    d, _ = repo
    assert latest_release_tag(str(d)) is None


def test_tag_and_commit_exists(repo):
    d, g = repo
    g("tag", "-a", TAG, "-m", "r")
    head = g("rev-parse", "HEAD")
    assert tag_exists(str(d), TAG) is True
    assert tag_exists(str(d), "99.99.999") is False
    assert commit_exists(str(d), head) is True
    assert commit_exists(str(d), "0" * 40) is False


def test_local_tag_commit_derefs_annotated(repo):
    d, g = repo
    g("tag", "-a", TAG, "-m", "r")
    assert local_tag_commit(str(d), TAG) == g("rev-parse", "HEAD")


def test_origin_owner_repo_https_and_ssh(repo, tmp_path):
    d, g = repo
    assert origin_owner_repo(str(d)) == OWNER_REPO
    g("remote", "set-url", "origin", f"git@github.com:{OWNER_REPO}.git")
    assert origin_owner_repo(str(d)) == OWNER_REPO


def test_github_tag_commit_annotated_chain(repo):
    d, g = repo
    g("tag", "-a", TAG, "-m", "r")
    head = g("rev-parse", "HEAD")
    http = _http(_published_routes(str(d), g))
    assert github_tag_commit(OWNER_REPO, TAG, http_get=http) == head


# -- verify_published_release ------------------------------------------------


def test_verify_ok(repo):
    d, g = repo
    g("tag", "-a", TAG, "-m", "r")
    ok, reason = verify_published_release(
        str(d), OWNER_REPO, TAG, http_get=_http(_published_routes(str(d), g))
    )
    assert ok, reason


def test_verify_rejects_draft(repo):
    d, g = repo
    g("tag", "-a", TAG, "-m", "r")
    ok, reason = verify_published_release(
        str(d), OWNER_REPO, TAG,
        http_get=_http(_published_routes(str(d), g, draft=True)),
    )
    assert not ok and "draft" in reason


def test_verify_rejects_prerelease(repo):
    d, g = repo
    g("tag", "-a", TAG, "-m", "r")
    ok, reason = verify_published_release(
        str(d), OWNER_REPO, TAG,
        http_get=_http(_published_routes(str(d), g, prerelease=True)),
    )
    assert not ok and "pre-release" in reason


def test_verify_rejects_404(repo):
    d, g = repo
    g("tag", "-a", TAG, "-m", "r")
    ok, reason = verify_published_release(
        str(d), OWNER_REPO, TAG, http_get=_http({f"/releases/tags/{TAG}": (404, None)})
    )
    assert not ok and "no published" in reason.lower()


def test_verify_rejects_unreachable(repo):
    d, g = repo
    g("tag", "-a", TAG, "-m", "r")
    ok, reason = verify_published_release(
        str(d), OWNER_REPO, TAG, http_get=_http({})  # everything → (0, None)
    )
    assert not ok and "unreachable" in reason.lower()


def test_verify_rejects_sha_mismatch(repo):
    d, g = repo
    g("tag", "-a", TAG, "-m", "r")
    tag_obj = g("rev-parse", TAG)
    routes = {
        f"/releases/tags/{TAG}": (200, {"draft": False, "prerelease": False}),
        f"/git/ref/tags/{TAG}": (200, {"object": {"type": "tag", "sha": tag_obj}}),
        f"/git/tags/{tag_obj}": (200, {"object": {"sha": "f" * 40}}),  # wrong commit
    }
    ok, reason = verify_published_release(str(d), OWNER_REPO, TAG, http_get=_http(routes))
    assert not ok and "!=" in reason


# -- resolve_and_verify ------------------------------------------------------


def test_resolve_empty_uses_latest_release_verified(repo):
    d, g = repo
    g("tag", "-a", TAG, "-m", "r")
    dec = resolve_and_verify(str(d), "", http_get=_http(_published_routes(str(d), g)))
    assert dec.error == "" and dec.ref == TAG and dec.kind == "release" and dec.verified


def test_resolve_no_tags_errors(repo):
    d, _ = repo
    dec = resolve_and_verify(str(d), "", http_get=_http({}))
    assert dec.error and dec.ref == ""


def test_resolve_tag_verify_failure_aborts(repo):
    d, g = repo
    g("tag", "-a", TAG, "-m", "r")
    dec = resolve_and_verify(
        str(d), TAG, http_get=_http(_published_routes(str(d), g, draft=True))
    )
    assert dec.error and dec.ref == "" and dec.kind == "release"


def test_resolve_force_skips_verification(repo):
    d, g = repo
    g("tag", "-a", TAG, "-m", "r")
    # http would fail (unreachable) but force bypasses it.
    dec = resolve_and_verify(str(d), TAG, force=True, http_get=_http({}))
    assert dec.error == "" and dec.ref == TAG and dec.kind == "release" and not dec.verified


def test_resolve_commit_pin(repo):
    d, g = repo
    head = g("rev-parse", "HEAD")
    dec = resolve_and_verify(str(d), head, http_get=_http({}))
    assert dec.error == "" and dec.ref == head and dec.kind == "commit" and not dec.verified


def test_resolve_unknown_target_errors(repo):
    d, _ = repo
    dec = resolve_and_verify(str(d), "nope-not-a-ref", http_get=_http({}))
    assert dec.error and dec.ref == ""
