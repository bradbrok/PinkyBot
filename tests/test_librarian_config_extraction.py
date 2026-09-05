"""Pin the configuration captured from run() before extracting its builder."""

import json
from dataclasses import asdict
from pathlib import Path

from pinky_daemon.librarian_runner import LibrarianRunner


def test_librarian_config_preserves_other_curation_settings(tmp_path):
    expected = json.loads(
        (Path(__file__).parent / "fixtures/librarian_sdk_config_before_bounds.json").read_text()
    )
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": expected["mcp_servers"]}))
    config = LibrarianRunner._build_sdk_config(str(tmp_path), expected["system_prompt"])
    expected["working_dir"] = str(tmp_path)
    # The bounds intentionally change the MCP surface and permission mode.
    # All other pre-extraction curation settings retain their captured values;
    # test_librarian_config_is_bounded independently pins the new bound fields.
    expected["mcp_servers"] = {"pinky-self": expected["mcp_servers"]["pinky-self"]}
    expected["permission_mode"] = "dontAsk"
    actual = asdict(config)
    assert {name: actual[name] for name in expected} == expected
