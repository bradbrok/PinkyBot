import os
import shlex
import subprocess
from pathlib import Path

import yaml

WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "release.yml"


def _release_steps() -> list[dict[str, object]]:
    workflow = yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)
    return workflow["jobs"]["release"]["steps"]


def test_run_blocks_do_not_contain_workflow_expressions():
    workflow = yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)

    for job in workflow["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", "")


def test_release_notes_are_written_literally_to_release_body_file(tmp_path):
    steps = _release_steps()
    generate = next(step for step in steps if step.get("name") == "Generate release notes")
    publish = next(step for step in steps if step.get("name") == "Create GitHub Release")

    assert generate["env"]["CUSTOM_NOTES"] == "${{ inputs.release_notes }}"
    assert "GITHUB_OUTPUT" not in generate["run"]
    assert publish["with"]["body_path"] == "/tmp/notes.md"
    assert "body" not in publish["with"]

    notes_path = tmp_path / "notes.md"
    marker_path = tmp_path / "shell-expansion-ran"
    script = generate["run"].replace(
        "> /tmp/notes.md", f"> {shlex.quote(str(notes_path))}"
    )
    custom_notes = (
        "# Literal custom notes\n"
        "`printf BACKTICK_EXPANDED`\n"
        f"$(touch {shlex.quote(str(marker_path))})\n"
        '"double quotes" and \'single quotes\'\n'
        "NOTES_EOF\n"
        "- final line"
    )
    env = os.environ | {
        "CUSTOM_NOTES": custom_notes,
        "RELEASE_VERSION": "26.08.999",
    }

    subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env=env,
        check=True,
    )

    assert not marker_path.exists()
    assert custom_notes in notes_path.read_text()
    assert "\nNOTES_EOF\n" in notes_path.read_text()
