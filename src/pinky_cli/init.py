"""pinky init -- scaffold a new Pinky project."""

from __future__ import annotations

import shutil
from pathlib import Path


def run_init(name: str = "Pinky", directory: str = ".") -> None:
    project_dir = Path(directory).resolve()
    templates_dir = Path(__file__).parent.parent.parent / "templates"

    print(f"Initializing Pinky project in {project_dir}...")

    # Create data directory
    data_dir = project_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Create CLAUDE.md from template
    claude_md = project_dir / "CLAUDE.md"
    if not claude_md.exists():
        template = templates_dir / "CLAUDE.md.template"
        if template.exists():
            content = template.read_text()
            content = content.replace("{{AGENT_NAME}}", name)
            content = content.replace("{{EMOTICON}}", ":)")
            content = content.replace("{{USER_NAME}}", "Your Name")
            content = content.replace("{{TIMEZONE}}", "UTC")
            content = content.replace("{{USER_DESCRIPTION}}", "Tell your AI about yourself")
            content = content.replace("{{PRIMARY_CHANNEL}}", "Telegram")
            claude_md.write_text(content)
            print(f"  Created {claude_md}")
        else:
            print("  Warning: CLAUDE.md template not found")
    else:
        print(f"  Skipped {claude_md} (already exists)")

    # Create pinky.yaml from example
    config = project_dir / "pinky.yaml"
    if not config.exists():
        example = templates_dir / "pinky.yaml.example"
        if example.exists():
            shutil.copy(example, config)
            print(f"  Created {config}")
        else:
            print("  Warning: pinky.yaml.example not found")
    else:
        print(f"  Skipped {config} (already exists)")

    # Create .gitignore
    gitignore = project_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "data/\n"
            "*.db\n"
            "*.db-wal\n"
            "*.db-shm\n"
            ".env\n"
            "__pycache__/\n"
            "*.pyc\n"
        )
        print(f"  Created {gitignore}")

    print()
    print("Next steps:")
    print(f"  1. Edit CLAUDE.md -- give {name} a personality")
    print("  2. Edit pinky.yaml -- add your API keys")
    print("  3. Run: pinky serve")
    print("  4. Run: pinky connect")
    print("  5. Run: claude")
