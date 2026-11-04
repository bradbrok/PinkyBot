"""Split a skill's text into sections and render a capped or single-section load.

Long skills fill the context window when loaded whole. An agent can opt in to a
cap: a skill longer than the cap then loads as its contents list, the first part
(whole sections from the top that fit the cap) and every section whose heading
ends with `` [always]``. Any other section is one ``section=`` call away.

Pure functions only (no I/O). With no cap, every output here is exactly the
whole-skill text ``load_skill`` has always returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SKILL_LOAD_CAP_MIN = 2000
SKILL_LOAD_CAP_MAX = 200_000

ALWAYS_MARKER = " [always]"
INTRO_LABEL = "(intro)"

# A line ending is LF, CRLF or a lone CR. Each match is one line with its ending.
_LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")
# 0-3 spaces, exactly one or two '#', one space, then the rest of the line.
_HEADING = re.compile(r" {0,3}#{1,2} (.*)")
_FENCE_OPEN = re.compile(r"`{3,}|~{3,}")
_CLOSING_HASHES = re.compile(r"(?:^|[ \t])#+\Z")
_DIGITS = re.compile(r"[0-9]+")


def validate_skill_load_cap(value: object) -> int:
    """Return a valid cap (0 = off, else 2000..200000) or raise ValueError."""
    if type(value) is not int or (
        value != 0 and not SKILL_LOAD_CAP_MIN <= value <= SKILL_LOAD_CAP_MAX
    ):
        raise ValueError(
            "skill_load_cap must be 0 (whole skill) or a whole number from "
            f"{SKILL_LOAD_CAP_MIN} to {SKILL_LOAD_CAP_MAX}"
        )
    return value


def effective_skill_load_cap(value: object) -> int:
    """The cap to apply: any value that is not a valid cap means 0 (whole skill)."""
    try:
        return validate_skill_load_cap(value)
    except ValueError:
        return 0


@dataclass(frozen=True)
class Section:
    """One part of a skill. Section 0 is the text before the first heading."""

    number: int
    text: str
    heading: str = ""  # Heading text as shown in the contents list; "" for section 0.
    always: bool = False

    @property
    def listed(self) -> bool:
        """Section 0 is listed only when it has non-space text; headings always are."""
        return self.number > 0 or bool(self.text.strip())

    @property
    def label(self) -> str:
        return self.heading if self.number > 0 else INTRO_LABEL


def _heading_text(rest: str) -> str:
    text = rest.strip()
    closing = _CLOSING_HASHES.search(text)
    if closing:
        text = text[: closing.start()]
    return text.strip()


def split_sections(text: str) -> list[Section]:
    """Split at '#'/'##' heading lines outside fenced code blocks.

    The returned sections always start with section 0 (possibly empty), and
    joining their texts in order gives back ``text`` exactly.
    """
    starts: list[tuple[int, str]] = []
    fence: tuple[str, int] | None = None
    offset = 0
    for line in _LINE.findall(text):
        content = line.rstrip("\r\n")
        if fence is not None:
            stripped = content.strip(" \t")
            char, size = fence
            if len(stripped) >= size and stripped == char * len(stripped):
                fence = None
        else:
            opening = _FENCE_OPEN.match(content.lstrip(" \t"))
            if opening:
                fence = (opening.group()[0], len(opening.group()))
            else:
                heading = _HEADING.fullmatch(content)
                if heading and heading.group(1).strip():
                    starts.append((offset, _heading_text(heading.group(1))))
        offset += len(line)

    first = starts[0][0] if starts else len(text)
    sections = [Section(0, text[:first])]
    for index, (start, heading) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        sections.append(
            Section(index + 1, text[start:end], heading, heading.endswith(ALWAYS_MARKER))
        )
    return sections


def _header(name: str, description) -> str:
    header = f"# Skill: {name}"
    if description:
        header += f"\n{description}"
    return header


def full_text(name: str, description, directive) -> str:
    """The whole-skill output, exactly as load_skill has always returned it."""
    return f"{_header(name, description)}\n\n{directive}"


def _ranges(numbers: list[int]) -> str:
    runs: list[list[int]] = []
    for number in numbers:
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def _contents(sections: list[Section], served: set[int] | None = None) -> str:
    lines = ["Contents:"]
    for section in sections:
        if not section.listed:
            continue
        line = f"{section.number}. {section.label} - {len(section.text)} characters"
        if served is not None and section.number in served:
            line += " - shown"
        lines.append(line)
    return "\n".join(lines)


def _gap_line(previous: str, numbers: list[int]) -> str:
    newline = "" if not previous or previous.endswith(("\n", "\r")) else "\n"
    return f"{newline}[Not shown: sections {_ranges(numbers)}]\n"


def render_default(name: str, description, directive, cap: int) -> str:
    """Load without a section argument: the whole skill, or a partial load over the cap."""
    whole = full_text(name, description, directive)
    if (
        type(cap) is not int
        or cap <= 0
        or not isinstance(directive, str)
        or len(directive) <= cap
    ):
        return whole
    sections = split_sections(directive)
    if len(sections) < 3:  # Section 0 plus fewer than two headings.
        return whole

    served = {0, 1}
    counted = len(sections[0].text) + (0 if sections[1].always else len(sections[1].text))
    for section in sections[2:]:
        if section.always:
            served.add(section.number)
        elif counted + len(section.text) <= cap:
            served.add(section.number)
            counted += len(section.text)
        else:
            break
    served.update(section.number for section in sections if section.always)
    if len(served) == len(sections):
        return whole

    not_shown = [section.number for section in sections if section.number not in served]
    served_chars = sum(len(section.text) for section in sections if section.number in served)
    notice = (
        f"[Partial load: {served_chars} of {len(directive)} characters (cap {cap}). "
        f"Not shown: sections {_ranges(not_shown)}. Read one with load_skill(\"{name}\", "
        f"section=\"<number or heading>\"), or section=\"all\" for the full text."
    )
    if len(sections[1].text) > cap:
        notice += " Section 1 is longer than the cap and is shown whole."
    notice += "]"

    body: list[str] = []
    gap: list[int] = []
    for section in sections:
        if section.number in served:
            if gap:
                body.append(_gap_line(body[-1] if body else "", gap))
                gap = []
            body.append(section.text)
        else:
            gap.append(section.number)
    if gap:
        body.append(_gap_line(body[-1] if body else "", gap))

    header = _header(name, description)
    partial = f"{header}\n\n{notice}\n\n{_contents(sections, served)}\n\n{''.join(body)}"
    # The notice and contents list can cost more than a small hidden part, and
    # a partial load must never be longer than the whole skill.
    if len(partial) >= len(whole):
        return whole
    return partial


def render_section(name: str, description, directive, section: str) -> str:
    """Load with a section argument: "all", a section number or an exact heading."""
    wanted = section.strip()
    if wanted == "all" or not isinstance(directive, str):
        return full_text(name, description, directive)
    sections = split_sections(directive)
    last = len(sections) - 1

    if _DIGITS.fullmatch(wanted):
        significant = wanted.lstrip("0") or "0"
        number = int(significant) if len(significant) <= 9 else -1
        matches = [s for s in sections if s.number == number and s.listed]
    else:
        matches = [s for s in sections[1:] if s.heading == wanted]
        if len(matches) > 1:
            numbers = ", ".join(str(s.number) for s in matches)
            return (
                f"Heading '{wanted}' appears {len(matches)} times in '{name}'; "
                f"use its number: {numbers}."
            )
    if not matches:
        return f"Section '{wanted}' not found in '{name}'.\n\n{_contents(sections)}"
    found = matches[0]
    return f"# Skill: {name} - section {found.number} of {last}: {found.label}\n\n{found.text}"
