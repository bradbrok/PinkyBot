"""Splitting a skill into sections and rendering a capped or single-section load."""

from __future__ import annotations

import pytest

from pinky_daemon.skill_sections import (
    effective_skill_load_cap,
    full_text,
    render_default,
    render_section,
    split_sections,
    validate_skill_load_cap,
)
from tests.skill_sections_support import FIXTURES, LONG, today_output

INTRO = "Intro.\n"


def sec(heading: str, size: int, level: str = "##") -> str:
    """One section of exactly ``size`` characters, starting with its heading line."""
    head = f"{level} {heading}\n"
    assert size > len(head) + 1
    return head + "x" * (size - len(head) - 1) + "\n"


def headings(text: str) -> list[str]:
    return [s.heading for s in split_sections(text)[1:]]


def notice(name, served, total, cap, not_shown, extra=""):
    return (
        f"[Partial load: {served} of {total} characters (cap {cap}). "
        f"Not shown: sections {not_shown}. Read one with load_skill(\"{name}\", "
        f"section=\"<number or heading>\"), or section=\"all\" for the full text.{extra}]"
    )


# ── Splitting ──────────────────────────────────────────────────────────────

ROUND_TRIP = {
    "backtick fence": ("# A\n```\n# x\n## y\n```\n## B\n", ["A", "B"]),
    "tilde fence": ("# A\n~~~\n# x\n## y\n~~~\n## B\n", ["A", "B"]),
    "longer fence not closed by shorter": ("# A\n````\n```\n# x\n````\n## B\n", ["A", "B"]),
    "other fence char does not close": ("# A\n~~~\n```\n# x\n~~~\n## B\n", ["A", "B"]),
    "closing line with text does not close": (
        "# A\n```\n``` more\n# x\n```\n## B\n", ["A", "B"],
    ),
    "indented fence opens": ("# A\n      ```\n# x\n      ```\n## B\n", ["A", "B"]),
    "two backticks are not a fence": ("# A\n``\n# B\n", ["A", "B"]),
    "unclosed fence runs to the end": ("# A\n```\n## x\n# y\n", ["A"]),
    "crlf": ("Intro\r\n# A\r\nbody\r\n## B\r\nmore\r\n", ["A", "B"]),
    "lone cr": ("# A\rbody\r## B\r", ["A", "B"]),
    "mixed endings": ("# A\r\nx\n## B\ry\r\n", ["A", "B"]),
    "0-3 spaces and 4 spaces": (
        "   # Three\n    # Four\n  ## Two\n# One\n", ["Three", "Two", "One"],
    ),
    "hash tag is not a heading": ("#tag\n# Real\n#another\n##also\n", ["Real"]),
    "tab after hash is not a heading": ("#\tTab\n# Real\n", ["Real"]),
    "empty heading line is not a heading": ("# \n#\n# Real\n", ["Real"]),
    "closing hashes": ("# Title ##\n## C#\n## Both # ##\n", ["Title", "C#", "Both #"]),
    "deeper headings stay inside": ("# A\n### Deep\n#### Deeper\n## B\n###### six\n", ["A", "B"]),
    "no trailing newline": ("# A\nbody", ["A"]),
    "empty": ("", []),
    "no heading": ("just text\nmore text\n", []),
}


@pytest.mark.parametrize("case", sorted(ROUND_TRIP))
def test_split_round_trips_and_finds_headings(case):
    text, expected = ROUND_TRIP[case]
    sections = split_sections(text)
    assert "".join(s.text for s in sections) == text
    assert headings(text) == expected
    assert [s.number for s in sections] == list(range(len(sections)))


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_split_round_trips_fixture_skills(name):
    directive = FIXTURES[name][1]
    assert "".join(s.text for s in split_sections(directive)) == directive


def test_crlf_sections_keep_their_line_endings():
    sections = split_sections("Intro\r\n# A\r\nbody\r\n## B\r\nmore\r\n")
    assert [s.text for s in sections] == ["Intro\r\n", "# A\r\nbody\r\n", "## B\r\nmore\r\n"]


def test_deeper_heading_lines_are_part_of_their_section():
    sections = split_sections("# A\n### Deep\ntext\n## B\n")
    assert sections[1].text == "# A\n### Deep\ntext\n"
    assert sections[2].text == "## B\n"


def test_fenced_heading_lines_are_part_of_their_section():
    text = "# A\n```\n# x\n```\n## B\n"
    assert split_sections(text)[1].text == "# A\n```\n# x\n```\n"


@pytest.mark.parametrize(
    "line,heading,always",
    [
        ("## Rules [always]", "Rules [always]", True),
        ("## Rules [always] ##", "Rules [always]", True),
        ("# Never do this [always]", "Never do this [always]", True),
        ("## Rules [Always]", "Rules [Always]", False),
        ("## Rules[always]", "Rules[always]", False),
        ("## [always]", "[always]", False),
        ("## Rules [always] extra", "Rules [always] extra", False),
    ],
)
def test_always_marker(line, heading, always):
    section = split_sections(f"{line}\nbody\n")[1]
    assert (section.heading, section.always) == (heading, always)


def test_section_zero_listed_only_with_text():
    assert split_sections("Intro\n# A\n")[0].listed
    assert not split_sections("\n \n# A\n")[0].listed
    assert not split_sections("# A\n")[0].listed
    assert split_sections("# A\n")[0].text == ""


# ── Default (capped) output ────────────────────────────────────────────────


def test_partial_exact_format():
    s1, s2, s3, s4 = sec("One", 100), sec("Two", 100), sec("Three", 100), sec("Four [always]", 100)
    text = INTRO + s1 + s2 + s3 + s4
    assert len(text) == 407
    expected = (
        "# Skill: demo\nDemo description\n\n"
        + notice("demo", 307, 407, 210, "3")
        + "\n\nContents:\n"
        "0. (intro) - 7 characters - shown\n"
        "1. One - 100 characters - shown\n"
        "2. Two - 100 characters - shown\n"
        "3. Three - 100 characters\n"
        "4. Four [always] - 100 characters - shown\n\n"
        + INTRO + s1 + s2 + "[Not shown: sections 3]\n" + s4
    )
    assert render_default("demo", "Demo description", text, 210) == expected


def test_partial_without_description_follows_todays_header_rule():
    text = INTRO + sec("One", 100) + sec("Two", 100) + sec("Three", 100)
    out = render_default("demo", "", text, 150)
    assert out.startswith("# Skill: demo\n\n[Partial load: ")
    assert out.isascii()


def test_contiguous_fill_up_to_the_cap():
    s = [sec(f"S{k}", 100) for k in range(1, 5)]
    text = INTRO + "".join(s)
    out = render_default("demo", "", text, 310)
    assert notice("demo", 307, 407, 310, "4") in out
    assert out.endswith(INTRO + s[0] + s[1] + s[2] + "[Not shown: sections 4]\n")


def test_fill_stops_at_first_section_that_does_not_fit():
    s1, s2, s3 = sec("One", 100), sec("Big", 300), sec("Small", 50)
    text = INTRO + s1 + s2 + s3
    out = render_default("demo", "", text, 200)
    # Section 3 alone would fit, but the first part stays contiguous.
    assert s3 not in out
    assert notice("demo", 107, 457, 200, "2-3") in out
    assert out.endswith(INTRO + s1 + "[Not shown: sections 2-3]\n")


def test_section_one_over_the_cap_is_shown_whole():
    s1, s2 = sec("Big", 500), sec("Small", 50)
    text = INTRO + s1 + s2
    out = render_default("demo", "", text, 300)
    assert s1 in out
    assert notice(
        "demo", 507, 557, 300, "2",
        " Section 1 is longer than the cap and is shown whole.",
    ) in out
    assert out.endswith(INTRO + s1 + "[Not shown: sections 2]\n")


def test_always_section_at_the_end_is_served_with_gap_lines():
    s = [sec("One", 100), sec("Two", 100), sec("Three", 100), sec("Four", 100)]
    rules = sec("Rules [always]", 100)
    text = INTRO + "".join(s) + rules
    out = render_default("demo", "", text, 210)
    assert rules in out
    assert out.endswith(
        INTRO + s[0] + s[1] + "[Not shown: sections 3-4]\n" + rules
    )
    assert "5. Rules [always] - 100 characters - shown" in out


def test_always_sections_do_not_use_up_the_cap():
    s1, rules = sec("One", 100), sec("Rules [always]", 150)
    s3, s4 = sec("Three", 100), sec("Four", 100)
    text = INTRO + s1 + rules + s3 + s4
    out = render_default("demo", "", text, 220)
    # 7 + 100 + 100 = 207 <= 220 once the always section is not counted.
    assert out.endswith(INTRO + s1 + rules + s3 + "[Not shown: sections 4]\n")
    assert notice("demo", 357, 457, 220, "4") in out


def test_several_gaps_and_ranges():
    s = [sec(f"S{k}", 100) for k in range(1, 8)]
    s[3] = sec("S4 [always]", 100)
    text = INTRO + "".join(s)
    out = render_default("demo", "", text, 150)
    assert notice("demo", 207, 707, 150, "2-3, 5-7") in out
    assert out.endswith(
        INTRO + s[0] + "[Not shown: sections 2-3]\n" + s[3] + "[Not shown: sections 5-7]\n"
    )


def test_unlisted_intro_is_not_in_contents():
    s = [sec("A", 100), sec("B", 100), sec("C", 100)]
    text = "\n" + "".join(s)
    out = render_default("demo", "", text, 150)
    contents = out.split("Contents:\n", 1)[1].split("\n\n", 1)[0]
    assert contents.splitlines() == [
        "1. A - 100 characters - shown",
        "2. B - 100 characters",
        "3. C - 100 characters",
    ]
    assert out.endswith("\n\n" + "\n" + s[0] + "[Not shown: sections 2-3]\n")


@pytest.mark.parametrize("cap", [0, -5])
def test_cap_zero_or_below_is_todays_output(cap):
    assert render_default("demo", "desc", LONG, cap) == today_output("demo", "desc", LONG)


def test_total_within_cap_is_todays_output():
    text = INTRO + sec("One", 100) + sec("Two", 100)
    assert render_default("demo", "d", text, len(text)) == today_output("demo", "d", text)
    assert render_default("demo", "d", text, len(text) - 1) != today_output("demo", "d", text)


@pytest.mark.parametrize(
    "text",
    ["x" * 5000, "# Only\n" + "x" * 5000, "Intro\n" + "x" * 5000 + "\n## Only\nmore\n",
     "# A\n```\n## inside\n```\n" + "x" * 5000],
)
def test_fewer_than_two_headings_is_todays_output(text):
    assert render_default("demo", "d", text, 100) == today_output("demo", "d", text)


def test_served_set_of_every_section_is_todays_output():
    text = INTRO + sec("One", 100) + sec("Rules [always]", 300)
    assert render_default("demo", "d", text, 200) == today_output("demo", "d", text)


def test_full_text_is_todays_output():
    for name, (description, directive) in FIXTURES.items():
        assert full_text(name, description, directive) == today_output(name, description, directive)
    assert full_text("demo", None, "body") == today_output("demo", None, "body")


# ── The section argument ───────────────────────────────────────────────────

S1, S2, S3 = sec("Setup", 100), sec("Usage", 100), sec("Rules [always]", 100)
DOC = INTRO + S1 + S2 + S3
CONTENTS = (
    "Contents:\n"
    "0. (intro) - 7 characters\n"
    "1. Setup - 100 characters\n"
    "2. Usage - 100 characters\n"
    "3. Rules [always] - 100 characters"
)


def test_section_by_number():
    assert render_section("demo", "d", DOC, "2") == (
        "# Skill: demo - section 2 of 3: Usage\n\n" + S2
    )
    assert render_section("demo", "d", DOC, "0") == (
        "# Skill: demo - section 0 of 3: (intro)\n\n" + INTRO
    )


def test_section_by_heading():
    assert render_section("demo", "d", DOC, "Usage") == render_section("demo", "d", DOC, "2")
    assert render_section("demo", "d", DOC, "  Usage \n") == render_section("demo", "d", DOC, "2")


def test_section_by_heading_with_always_marker():
    assert render_section("demo", "d", DOC, "Rules [always]") == (
        "# Skill: demo - section 3 of 3: Rules [always]\n\n" + S3
    )
    assert render_section("demo", "d", DOC, "Rules").startswith("Section 'Rules' not found")


def test_heading_match_is_case_sensitive():
    assert render_section("demo", "d", DOC, "usage") == (
        "Section 'usage' not found in 'demo'.\n\n" + CONTENTS
    )


def test_not_found_lists_contents():
    assert render_section("demo", "d", DOC, "Nope") == (
        "Section 'Nope' not found in 'demo'.\n\n" + CONTENTS
    )


@pytest.mark.parametrize("arg", ["4", "99", "00004", "9" * 5000])
def test_number_out_of_range_is_not_found(arg):
    assert render_section("demo", "d", DOC, arg) == (
        f"Section '{arg}' not found in 'demo'.\n\n" + CONTENTS
    )


def test_section_zero_only_when_listed():
    text = S1 + S2
    assert render_section("demo", "d", text, "0").startswith("Section '0' not found in 'demo'.")


def test_duplicate_heading_is_refused():
    text = INTRO + sec("Notes", 100) + sec("Other", 100) + sec("Notes", 100)
    assert render_section("demo", "d", text, "Notes") == (
        "Heading 'Notes' appears 2 times in 'demo'; use its number: 1, 3."
    )
    assert render_section("demo", "d", text, "3").startswith(
        "# Skill: demo - section 3 of 3: Notes\n\n"
    )


@pytest.mark.parametrize("arg", ["all", " all "])
def test_section_all_is_todays_output(arg):
    assert render_section("demo", "d", LONG, arg) == today_output("demo", "d", LONG)


def test_section_text_is_whole_whatever_its_size():
    big = sec("Huge", 50_000)
    text = INTRO + sec("One", 100) + big
    assert render_section("demo", "", text, "2") == "# Skill: demo - section 2 of 2: Huge\n\n" + big


# ── Cap values ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [0, 2000, 20000, 200000])
def test_valid_caps(value):
    assert validate_skill_load_cap(value) == value
    assert effective_skill_load_cap(value) == value


@pytest.mark.parametrize(
    "value", [1, 1999, 200001, -1, True, False, "x", "2000", 2000.0, None, [2000]]
)
def test_invalid_caps(value):
    with pytest.raises(ValueError, match="skill_load_cap"):
        validate_skill_load_cap(value)
    assert effective_skill_load_cap(value) == 0
