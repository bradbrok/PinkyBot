"""Pin the configuration captured from run() before extracting its builder."""

import json
from dataclasses import asdict
from pathlib import Path

from pinky_daemon.librarian_runner import LibrarianRunner


def test_librarian_config_matches_pre_extraction_snapshot(tmp_path):
    expected = json.loads(
        (Path(__file__).parent / "fixtures/librarian_sdk_config_before_bounds.json").read_text()
    )
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": expected["mcp_servers"]}))
    config = LibrarianRunner._build_sdk_config(str(tmp_path), expected["system_prompt"])
    expected["working_dir"] = str(tmp_path)
    assert asdict(config) == expected
