"""Shared skill texts and a frozen copy of the original load_skill output."""

from __future__ import annotations


def filler(label: str, size: int) -> str:
    """Return about ``size`` characters of plain lines (no headings, no fences)."""
    line = f"{label} instructions line for the fixture skill.\n"
    return line * max(1, size // len(line))


def today_output(name: str, description, directive) -> str:
    """Frozen copy of load_skill's success output before section loading."""
    header = f"# Skill: {name}"
    if description:
        header += f"\n{description}"
    return f"{header}\n\n{directive}"


# Over 20000 characters, several headings, a fence with heading-like lines,
# an always-served section and a long tail.
LONG = (
    "Intro paragraph for the fixture skill.\n\n"
    "# Main Title\n" + filler("main", 3000) + "\n"
    "## Setup\n" + filler("setup", 6000) + "\n"
    "## Usage\n"
    "```bash\n# not a heading\n## also not a heading\n```\n"
    + filler("usage", 8000) + "\n"
    "## Rules [always]\n" + filler("rules", 500) + "\n"
    "## Reference\n" + filler("reference", 9000)
)

SHORT = "# Short\n\nDo the thing.\n"
NO_HEADING = filler("plain", 25000)
ONE_HEADING = "# Only\n" + filler("only", 25000)
FENCES = (
    "# Fenced\n" + filler("a", 4000)
    + "```\n## inside backticks\n# also inside\n```\n"
    + "~~~\n# inside tildes\n```\n## still inside\n~~~\n"
    + filler("b", 4000)
    + "## After Fences\n" + filler("c", 9000)
    + "## Tail\n" + filler("d", 9000)
)
ALWAYS_LAST = (
    "# Start\n" + filler("start", 3000)
    + "## Middle\n" + filler("middle", 12000)
    + "## Later\n" + filler("later", 8000)
    + "## Never Do [always]\n" + filler("never", 400)
)

# name -> (description, directive)
FIXTURES = {
    "short": ("A short skill", SHORT),
    "long": ("A long skill", LONG),
    "no-heading": ("", NO_HEADING),
    "one-heading": ("Only one heading", ONE_HEADING),
    "fences": ("Fenced code", FENCES),
    "always-last": ("", ALWAYS_LAST),
}
