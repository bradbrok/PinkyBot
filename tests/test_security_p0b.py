"""Regression tests for the P0b hardening batch.

Covers path-traversal containment on skill creation (and, by the same
pattern, the upload endpoint). The skill-name check rejects before any
filesystem write, so these tests have no side effects.
"""
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from pinky_daemon.api import create_api


def _client():
    fd, path = tempfile.mkstemp(suffix=".db")
    import os
    os.close(fd)
    app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
    return TestClient(app), path


class TestSkillFromMdNameTraversal:
    """The skill name becomes a directory under skills/; a crafted frontmatter
    name must not traverse out of skills/ to write SKILL.md elsewhere."""

    def _post(self, client, name):
        content = f"---\nname: {name}\ndescription: test\n---\nbody\n"
        return client.post("/skills/from-md", json={"content": content})

    def test_relative_traversal_name_rejected(self):
        client, path = _client()
        sentinel = Path("/tmp/pinky_p0b_pwned_skill")
        resp = self._post(client, "../../../tmp/pinky_p0b_pwned_skill")
        assert resp.status_code == 400
        assert "invalid skill name" in resp.text.lower()
        assert not sentinel.exists()
        import os
        os.unlink(path)

    def test_absolute_path_name_rejected(self):
        client, path = _client()
        resp = self._post(client, "/tmp/pinky_p0b_abs")
        assert resp.status_code == 400
        import os
        os.unlink(path)

    def test_slash_in_name_rejected(self):
        client, path = _client()
        resp = self._post(client, "evil/nested")
        assert resp.status_code == 400
        import os
        os.unlink(path)

    def test_consecutive_hyphens_rejected(self):
        client, path = _client()
        resp = self._post(client, "bad--name")
        assert resp.status_code == 400
        import os
        os.unlink(path)
