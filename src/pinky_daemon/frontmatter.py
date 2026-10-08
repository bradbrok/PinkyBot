"""Linear splitting with the existing frontmatter fence and whitespace semantics."""


def split_frontmatter(text: str) -> tuple[str, str] | None:
    """Return the raw metadata and body, or None when there is no matching fence."""
    if not text.startswith("---"):
        return None
    end, newline, previous = 3, -1, -1
    while end < len(text) and text[end].isspace():
        if text[end] == "\n":
            previous, newline = newline, end
        end += 1
    if newline < 0:
        return None
    # Greedy opening whitespace takes the latest newline that permits a closing fence.
    close = text.find("\n---", newline + 1)
    if close < 0:
        # Only the last prefix newline can itself precede a fence; try its predecessor.
        if previous < 0 or not text.startswith("\n---", newline):
            return None
        close, newline = newline, previous
    body = close + 4
    while body < len(text) and text[body].isspace():
        body += 1
    return text[newline + 1:close], text[body:]
