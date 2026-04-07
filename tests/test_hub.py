"""Tests for pinky_hub hub_store and API endpoints."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from pinky_hub.hub_store import HubStore, Instance, PublicPresentation
from pinky_hub.api import create_hub_app


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    """In-memory-ish store using a temp file."""
    return HubStore(db_path=str(tmp_path / "test_hub.db"))


@pytest.fixture
def populated_store(store):
    """Store with one instance and two presentations."""
    inst = store.register_instance(
        label="Test Instance",
        url="http://localhost:9999",
        api_key="secret-key",
        owner_email="test@example.com",
        owner_name="Tester",
    )
    store.upsert_presentation(
        instance_id=inst.id,
        remote_id=1,
        title="Alpha Deck",
        description="First presentation",
        created_by="barsik",
        share_token="tok-alpha",
        tags=["tag1"],
        version=2,
        template="minimal",
        thumbnail_url="https://example.com/thumb1.png",
    )
    store.upsert_presentation(
        instance_id=inst.id,
        remote_id=2,
        title="Beta Deck",
        description="Second presentation",
        created_by="pushok",
        share_token="tok-beta",
        tags=["tag2", "tag3"],
        version=1,
        template="dark",
        thumbnail_url="",
    )
    return store, inst


@pytest.fixture
def client(tmp_path):
    """TestClient using a fresh hub app."""
    app = create_hub_app(db_path=str(tmp_path / "api_test_hub.db"))
    return TestClient(app)


@pytest.fixture
def client_with_instance(tmp_path):
    """TestClient with a pre-registered instance and presentations."""
    db = str(tmp_path / "api_test_hub.db")
    app = create_hub_app(db_path=db)
    store = HubStore(db_path=db)

    inst = store.register_instance(
        label="Pinky Alpha",
        url="http://alpha.pinky.local",
        api_key="alpha-key",
        owner_email="owner@alpha.com",
        owner_name="Alpha Owner",
    )
    store.upsert_presentation(
        instance_id=inst.id,
        remote_id=10,
        title="Quarterly Review",
        description="Q4 results",
        created_by="barsik",
        share_token="share-qr",
        tags=["business"],
        version=3,
        template="corporate",
        thumbnail_url="https://alpha.pinky.local/thumb/qr.png",
    )
    return TestClient(app), inst


# ── HubStore: Instance methods ────────────────────────────────


class TestHubStoreInstances:
    def test_register_and_get(self, store):
        inst = store.register_instance("My Pinky", "http://localhost:8888", "key123")
        assert inst.id > 0
        assert inst.label == "My Pinky"
        assert inst.url == "http://localhost:8888"
        assert inst.is_active is True
        assert inst.registered_at > 0
        assert inst.last_seen_at > 0

    def test_register_excludes_api_key_from_dict(self, store):
        inst = store.register_instance("Test", "http://test.local", "super-secret")
        d = inst.to_dict()
        assert "api_key" not in d

    def test_list_instances_active_only(self, store):
        inst = store.register_instance("Active", "http://active.local", "k1")
        inst2 = store.register_instance("Inactive", "http://inactive.local", "k2")
        store.deactivate_instance(inst2.id)

        active = store.list_instances(active_only=True)
        assert len(active) == 1
        assert active[0].id == inst.id

    def test_list_instances_all(self, store):
        store.register_instance("A", "http://a.local", "k1")
        inst2 = store.register_instance("B", "http://b.local", "k2")
        store.deactivate_instance(inst2.id)

        all_inst = store.list_instances(active_only=False)
        assert len(all_inst) == 2

    def test_update_last_seen(self, store):
        inst = store.register_instance("Seen", "http://seen.local", "k")
        before = inst.last_seen_at
        time.sleep(0.01)
        store.update_last_seen(inst.id)
        updated = store.get_instance_by_id(inst.id)
        assert updated.last_seen_at > before

    def test_get_instance_with_presentations(self, populated_store):
        store, inst = populated_store
        result = store.get_instance(inst.id)
        assert result is not None
        assert result["id"] == inst.id
        assert result["label"] == "Test Instance"
        assert "presentations" in result
        assert len(result["presentations"]) == 2
        titles = {p["title"] for p in result["presentations"]}
        assert titles == {"Alpha Deck", "Beta Deck"}

    def test_get_instance_returns_none_for_missing(self, store):
        assert store.get_instance(9999) is None

    def test_get_instance_stats(self, populated_store):
        store, _ = populated_store
        stats = store.get_instance_stats()
        assert stats["total_instances"] == 1
        assert stats["total_presentations"] == 2
        assert stats["total_agents"] == 2  # barsik + pushok

    def test_get_instance_stats_empty(self, store):
        stats = store.get_instance_stats()
        assert stats["total_instances"] == 0
        assert stats["total_presentations"] == 0
        assert stats["total_agents"] == 0


# ── HubStore: Presentation methods ───────────────────────────


class TestHubStorePresentations:
    def test_upsert_new(self, store):
        inst = store.register_instance("I", "http://i.local", "k")
        pres = store.upsert_presentation(
            instance_id=inst.id,
            remote_id=42,
            title="Deck One",
            description="Desc",
            created_by="agent",
            share_token="tok-one",
            tags=["a", "b"],
            version=1,
            template="light",
            thumbnail_url="http://thumb.example.com/1.png",
        )
        assert pres.id > 0
        assert pres.title == "Deck One"
        assert pres.tags == ["a", "b"]
        assert pres.template == "light"
        assert pres.thumbnail_url == "http://thumb.example.com/1.png"

    def test_upsert_updates_existing(self, store):
        inst = store.register_instance("I", "http://i.local", "k")
        store.upsert_presentation(inst.id, 1, "Old Title", "", "ag", "tok", [], 1)
        updated = store.upsert_presentation(inst.id, 1, "New Title", "Desc", "ag", "tok", [], 2)
        assert updated.title == "New Title"
        assert updated.version == 2

    def test_instance_label_populated(self, populated_store):
        store, inst = populated_store
        items = store.list_public_presentations()
        for p in items:
            assert p.instance_label == "Test Instance"

    def test_list_presentations_excludes_inactive_instance(self, store):
        inst = store.register_instance("Gone", "http://gone.local", "k")
        store.upsert_presentation(inst.id, 1, "T", "", "", "tok", [], 1)
        store.deactivate_instance(inst.id)
        assert store.list_public_presentations() == []

    def test_get_presentation_by_id(self, populated_store):
        store, inst = populated_store
        listings = store.list_public_presentations()
        first = listings[0]
        fetched = store.get_presentation_by_id(first.id)
        assert fetched is not None
        assert fetched.id == first.id
        assert fetched.instance_label == "Test Instance"

    def test_get_presentation_by_id_missing(self, store):
        assert store.get_presentation_by_id(9999) is None

    def test_get_presentation_by_token(self, populated_store):
        store, _ = populated_store
        pres = store.get_presentation_by_token("tok-alpha")
        assert pres is not None
        assert pres.title == "Alpha Deck"
        assert pres.instance_label == "Test Instance"

    def test_get_presentation_by_token_missing(self, store):
        assert store.get_presentation_by_token("nope") is None

    def test_template_and_thumbnail_in_dict(self, populated_store):
        store, _ = populated_store
        pres = store.get_presentation_by_token("tok-alpha")
        d = pres.to_dict()
        assert d["template"] == "minimal"
        assert d["thumbnail_url"] == "https://example.com/thumb1.png"
        assert d["instance_label"] == "Test Instance"

    def test_count_instances(self, populated_store):
        store, _ = populated_store
        assert store.count_instances() == 1

    def test_count_presentations(self, populated_store):
        store, _ = populated_store
        assert store.count_presentations() == 2

    def test_schema_migration(self, tmp_path):
        """Existing DB without template/thumbnail_url columns gets migrated."""
        import sqlite3

        db_path = str(tmp_path / "old.db")
        # Create old-style schema without new columns
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                owner_email TEXT NOT NULL DEFAULT '',
                owner_name TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                registered_at REAL NOT NULL,
                last_seen_at REAL NOT NULL
            );
            CREATE TABLE public_presentations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id INTEGER NOT NULL,
                remote_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                share_token TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                version INTEGER NOT NULL DEFAULT 1,
                synced_at REAL NOT NULL,
                UNIQUE(instance_id, remote_id)
            );
        """)
        conn.commit()
        conn.close()

        # Opening with HubStore should migrate without error
        store = HubStore(db_path=db_path)
        cols = {
            row[1]
            for row in store._db.execute(
                "PRAGMA table_info(public_presentations)"
            ).fetchall()
        }
        assert "template" in cols
        assert "thumbnail_url" in cols


# ── API endpoints ─────────────────────────────────────────────


class TestHubAPIRoot:
    def test_root(self, client):
        r = client.get("/")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "pinky-hub"
        assert "instance_count" in data
        assert "presentation_count" in data


class TestHubAPIInstances:
    def test_list_instances_empty(self, client):
        r = client.get("/instances")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_instances_returns_active_only(self, client_with_instance):
        client, inst = client_with_instance
        r = client.get("/instances")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["id"] == inst.id
        assert data[0]["label"] == "Pinky Alpha"
        assert "api_key" not in data[0]

    def test_get_instance_detail(self, client_with_instance):
        client, inst = client_with_instance
        r = client.get(f"/instances/{inst.id}")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == inst.id
        assert "presentations" in data
        assert len(data["presentations"]) == 1
        pres = data["presentations"][0]
        assert pres["title"] == "Quarterly Review"
        assert pres["template"] == "corporate"

    def test_get_instance_not_found(self, client):
        r = client.get("/instances/9999")
        assert r.status_code == 404

    def test_get_instance_no_api_key(self, client_with_instance):
        client, inst = client_with_instance
        r = client.get(f"/instances/{inst.id}")
        data = r.json()
        assert "api_key" not in data


class TestHubAPIStats:
    def test_stats_empty(self, client):
        r = client.get("/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["total_instances"] == 0
        assert data["total_presentations"] == 0
        assert data["total_agents"] == 0

    def test_stats_with_data(self, client_with_instance):
        client, _ = client_with_instance
        r = client.get("/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["total_instances"] == 1
        assert data["total_presentations"] == 1
        assert data["total_agents"] == 1  # barsik


class TestHubAPIPresentations:
    def test_list_presentations_empty(self, client):
        r = client.get("/presentations")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_presentations(self, client_with_instance):
        client, _ = client_with_instance
        r = client.get("/presentations")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["title"] == "Quarterly Review"
        assert data[0]["template"] == "corporate"
        assert data[0]["thumbnail_url"] == "https://alpha.pinky.local/thumb/qr.png"
        assert data[0]["instance_label"] == "Pinky Alpha"

    def test_list_presentations_pagination(self, client_with_instance):
        client, _ = client_with_instance
        r = client.get("/presentations?limit=1&offset=0")
        assert r.status_code == 200
        assert len(r.json()) == 1

        r2 = client.get("/presentations?limit=1&offset=1")
        assert r2.status_code == 200
        assert len(r2.json()) == 0

    def test_get_presentation_by_id(self, client_with_instance):
        client, _ = client_with_instance
        # First get the list to find the ID
        listings = client.get("/presentations").json()
        pres_id = listings[0]["id"]

        r = client.get(f"/presentations/{pres_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["title"] == "Quarterly Review"
        assert "view_url" in data
        assert "alpha.pinky.local" in data["view_url"]

    def test_get_presentation_by_id_not_found(self, client):
        r = client.get("/presentations/9999")
        assert r.status_code == 404

    def test_public_presentations_alias(self, client_with_instance):
        """Legacy /public/presentations route still works."""
        client, _ = client_with_instance
        r = client.get("/public/presentations")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1

    def test_public_presentation_by_token(self, client_with_instance):
        """Legacy token-based lookup still works."""
        client, _ = client_with_instance
        r = client.get("/public/presentations/share-qr")
        assert r.status_code == 200
        data = r.json()
        assert data["title"] == "Quarterly Review"
        assert "view_url" in data

    def test_public_presentation_token_not_found(self, client):
        r = client.get("/public/presentations/no-such-token")
        assert r.status_code == 404


class TestHubAPIHeartbeat:
    def test_heartbeat_updates_last_seen(self, client_with_instance):
        client, inst = client_with_instance
        r = client.post(f"/instances/{inst.id}/heartbeat")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["instance_id"] == inst.id
        assert "last_seen_at" in data

    def test_heartbeat_not_found(self, client):
        r = client.post("/instances/9999/heartbeat")
        assert r.status_code == 404


class TestHubAPIDeactivate:
    def test_deactivate(self, client_with_instance):
        client, inst = client_with_instance
        r = client.delete(f"/instances/{inst.id}")
        assert r.status_code == 200
        assert r.json()["ok"] is True

        # No longer in active list
        r2 = client.get("/instances")
        assert r2.json() == []

        # Stats should reflect removal
        stats = client.get("/stats").json()
        assert stats["total_instances"] == 0
        assert stats["total_presentations"] == 0

    def test_deactivate_not_found(self, client):
        r = client.delete("/instances/9999")
        assert r.status_code == 404


class TestHubAPISyncSkipsMalformed:
    def test_sync_skips_items_without_id(self, tmp_path):
        """Malformed items (missing 'id') are skipped gracefully during sync."""
        db = str(tmp_path / "sync_test.db")
        app = create_hub_app(db_path=db)
        store = HubStore(db_path=db)
        inst = store.register_instance("S", "http://s.local", "key-sync")

        with patch("pinky_hub.api._daemon_get") as mock_get:
            mock_get.return_value = [
                {"id": 1, "title": "Good", "share_token": "tok-good", "tags": [], "version": 1},
                {"title": "No ID here", "share_token": "tok-bad"},  # missing id — should skip
            ]
            client = TestClient(app)
            r = client.post(
                f"/instances/{inst.id}/sync", json={"api_key": "key-sync"}
            )

        assert r.status_code == 200
        assert r.json()["synced"] == 1

    def test_sync_passes_template_and_thumbnail(self, tmp_path):
        """Sync correctly stores template and thumbnail_url from daemon response."""
        db = str(tmp_path / "sync_meta_test.db")
        app = create_hub_app(db_path=db)
        store = HubStore(db_path=db)
        inst = store.register_instance("Meta", "http://meta.local", "key-meta")

        with patch("pinky_hub.api._daemon_get") as mock_get:
            mock_get.return_value = [
                {
                    "id": 5,
                    "title": "Rich Deck",
                    "description": "Full metadata",
                    "created_by": "barsik",
                    "share_token": "tok-rich",
                    "tags": ["demo"],
                    "version": 1,
                    "template": "gradient",
                    "thumbnail_url": "http://meta.local/thumbs/5.png",
                }
            ]
            client = TestClient(app)
            r = client.post(
                f"/instances/{inst.id}/sync", json={"api_key": "key-meta"}
            )

        assert r.status_code == 200
        assert r.json()["synced"] == 1

        pres = store.get_presentation_by_token("tok-rich")
        assert pres is not None
        assert pres.template == "gradient"
        assert pres.thumbnail_url == "http://meta.local/thumbs/5.png"
