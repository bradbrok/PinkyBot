"""Prove the additive approved-user migration using a copied fixture store."""

import shutil
import sqlite3

from pinky_daemon.agent_registry import AgentRegistry

NEW_COLUMNS = {"slack_auto_member_ids", "slack_auto_checked_at"}


def _state(path):
    with sqlite3.connect(path) as connection:
        columns = {
            row[1]: (row[2], row[3], row[4])
            for row in connection.execute("PRAGMA table_info(approved_users)")
        }
        rootpage = connection.execute(
            "SELECT rootpage FROM sqlite_master WHERE name='approved_users'",
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT id, agent_name, chat_id, display_name, status, approved_by, timezone, "
            "created_at, updated_at FROM approved_users ORDER BY id",
        ).fetchall()
    connection.close()
    return columns, rootpage, rows


def test_legacy_approved_users_table_is_extended_without_rewriting(tmp_path):
    legacy = tmp_path / "legacy.db"
    registry = AgentRegistry(str(legacy))
    try:
        registry.register("fixture-agent", working_dir=str(tmp_path))
        registry.approve_user("fixture-agent", "U000000011", "Fixture Member", "manual")
        registry.set_user_timezone("fixture-agent", "U000000011", "UTC")
        registry.add_pending_user("fixture-agent", "G000000011", "Pending Fixture")
        registry.deny_user("fixture-agent", "U000000012")
        public_rows = [user.to_dict() for user in registry.list_approved_users("fixture-agent")]
    finally:
        registry.close()

    # On stage builds, reconstruct the legacy schema in the source fixture only.
    # The copy is opened by the actual production initializer for both passes.
    columns, _, _ = _state(legacy)
    with sqlite3.connect(legacy) as connection:
        for name in sorted(NEW_COLUMNS & columns.keys()):
            connection.execute(f"ALTER TABLE approved_users DROP COLUMN {name}")
    connection.close()
    old_columns, old_rootpage, old_rows = _state(legacy)
    assert len(old_rows) == 3 and not (NEW_COLUMNS & old_columns.keys())
    copied = tmp_path / "migrating.db"
    shutil.copy2(legacy, copied)
    for _ in range(2):
        migrated = AgentRegistry(str(copied))
        try:
            assert [user.to_dict() for user in migrated.list_approved_users("fixture-agent")] == (
                public_rows
            )
        finally:
            migrated.close()
        new_columns, rootpage, rows = _state(copied)
        assert set(new_columns) - set(old_columns) == NEW_COLUMNS
        assert {name: new_columns[name] for name in old_columns} == old_columns
        assert rootpage == old_rootpage
        assert rows == old_rows
        with sqlite3.connect(copied) as connection:
            assert connection.execute(
                "SELECT slack_auto_member_ids, slack_auto_checked_at FROM approved_users",
            ).fetchall() == [(None, None)] * len(old_rows)
        connection.close()
