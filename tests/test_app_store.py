"""Tests for app_store.py — App CRUD operations."""

from __future__ import annotations

import pytest

from pinky_daemon.app_store import AppStore


@pytest.fixture
def store(tmp_path):
    return AppStore(db_path=str(tmp_path / "test_apps.db"))


def test_create_app(store):
    app = store.create("My Tool", description="A cool tool", app_type="tool")
    assert app.id == 1
    assert app.name == "My Tool"
    assert app.description == "A cool tool"
    assert app.app_type == "tool"
    assert app.status == "draft"
    assert app.slug == "my-tool"
    assert app.share_token


def test_create_duplicate_slug(store):
    a1 = store.create("Test App")
    a2 = store.create("Test App")
    assert a1.slug == "test-app"
    assert a2.slug == "test-app-1"


def test_get_app(store):
    created = store.create("Getter")
    fetched = store.get(created.id)
    assert fetched is not None
    assert fetched.name == "Getter"


def test_get_nonexistent(store):
    assert store.get(999) is None


def test_list_apps(store):
    store.create("A")
    store.create("B")
    store.create("C")
    apps = store.list()
    assert len(apps) == 3


def test_list_filter_status(store):
    store.create("Draft")
    app2 = store.create("Deployed")
    store.deploy(app2.id, "<h1>Hi</h1>")
    drafts = store.list(status="draft")
    deployed = store.list(status="deployed")
    assert len(drafts) == 1
    assert len(deployed) == 1


def test_update_app(store):
    app = store.create("Old Name")
    updated = store.update(app.id, name="New Name", description="Updated")
    assert updated is not None
    assert updated.name == "New Name"
    assert updated.description == "Updated"


def test_update_nonexistent(store):
    assert store.update(999, name="X") is None


def test_deploy_app(store):
    app = store.create("Deployer")
    deployed = store.deploy(app.id, "<html><body>Hello</body></html>")
    assert deployed is not None
    assert deployed.status == "deployed"
    assert deployed.html_content == "<html><body>Hello</body></html>"


def test_deploy_nonexistent(store):
    assert store.deploy(999, "<h1>x</h1>") is None


def test_regenerate_share_token(store):
    app = store.create("Sharer")
    old_token = app.share_token
    updated = store.regenerate_share_token(app.id)
    assert updated is not None
    assert updated.share_token != old_token


def test_delete_app(store):
    app = store.create("Deleter")
    assert store.delete(app.id) is True
    assert store.get(app.id) is None


def test_delete_nonexistent(store):
    assert store.delete(999) is False


def test_get_by_share_token(store):
    app = store.create("Token Lookup")
    found = store.get_by_share_token(app.share_token)
    assert found is not None
    assert found.id == app.id


def test_get_by_share_token_not_found(store):
    assert store.get_by_share_token("nonexistent") is None


def test_stats(store):
    store.create("A", app_type="tool")
    store.create("B", app_type="dashboard")
    app3 = store.create("C", app_type="tool")
    store.deploy(app3.id, "<h1>live</h1>")
    stats = store.get_stats()
    assert stats["total"] == 3
    assert stats["by_type"]["tool"] == 2
    assert stats["by_type"]["dashboard"] == 1
    assert stats["by_status"]["draft"] == 2
    assert stats["by_status"]["deployed"] == 1


def test_health_check_draft(store):
    app = store.create("Health Draft")
    health = store.check_health(app.id)
    assert health["ok"] is False
    assert health["status"] == "draft"


def test_health_check_deployed(store):
    app = store.create("Health Deployed")
    store.deploy(app.id, "<h1>Live</h1>")
    health = store.check_health(app.id)
    assert health["ok"] is True
    assert health["has_content"] is True


def test_health_check_nonexistent(store):
    health = store.check_health(999)
    assert health["ok"] is False
    assert health["error"] == "App not found"


def test_to_dict_excludes_html_by_default(store):
    app = store.create("Dict Test")
    store.deploy(app.id, "<h1>HTML</h1>")
    app = store.get(app.id)
    d = app.to_dict()
    assert "html_content" not in d
    d_with = app.to_dict(include_html=True)
    assert "html_content" in d_with
    assert d_with["html_content"] == "<h1>HTML</h1>"


def test_invalid_app_type_defaults_to_other(store):
    app = store.create("Invalid Type", app_type="banana")
    assert app.app_type == "other"


def test_list_with_tag_filter(store):
    store.create("Tagged", tags=["frontend", "tool"])
    store.create("Untagged")
    tagged = store.list(tag="frontend")
    assert len(tagged) == 1
    assert tagged[0].name == "Tagged"


def test_set_and_check_password(store):
    app = store.create("Protected")
    assert app.access_password == ""
    assert store.set_password(app.id, "secret123")
    assert store.check_password(app.id, "secret123") is True
    assert store.check_password(app.id, "wrong") is False


def test_remove_password(store):
    app = store.create("Was Protected")
    store.set_password(app.id, "secret")
    store.set_password(app.id, "")
    # No password means always pass
    assert store.check_password(app.id, "anything") is True


def test_password_nonexistent_app(store):
    assert store.set_password(999, "pass") is False


def test_to_dict_shows_protected_flag(store):
    app = store.create("PW Test")
    assert app.to_dict()["protected"] is False
    store.set_password(app.id, "secret")
    app = store.get(app.id)
    assert app.to_dict()["protected"] is True
    assert "access_password" not in app.to_dict()


def test_static_dir(store):
    app = store.create("Static App")
    d = store.get_static_dir(app.id)
    assert str(app.id) in str(d)
    assert not d.exists()
    d2 = store.ensure_static_dir(app.id)
    assert d2.exists()


def test_health_with_static_files(store):
    app = store.create("Static Health")
    store.deploy(app.id, "")
    # Deploy with empty content, no static dir
    health = store.check_health(app.id)
    assert health["has_content"] is False
    assert health["has_static_files"] is False
    # Create static dir with index
    d = store.ensure_static_dir(app.id)
    (d / "index.html").write_text("<h1>Hi</h1>")
    health = store.check_health(app.id)
    assert health["has_static_files"] is True
    assert health["ok"] is True
