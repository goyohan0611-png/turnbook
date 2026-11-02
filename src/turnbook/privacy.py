"""Explicit user-authored privacy delimiters, independent of secret masking."""
import re

PRIVATE_TAG = re.compile(r"</?private\s*>", re.IGNORECASE)
# a tag written as code (`<private>`, or in a ``` block) is talk about the feature, not a tag:
# unclosed, it dropped the rest of a message that only mentioned it. opencode_plugin.js too.
CODE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)


def strip_private(text: str) -> str:
    """Omit tagged blocks, including nested blocks and an unclosed block's remainder.

    Newlines separate public fragments so removal cannot join words into a different fact.
    This affects Turnbook's copy, not the source app's own transcript.
    """
    code = [span.span() for span in CODE.finditer(text)]
    parts, depth, start = [], 0, 0
    for match in PRIVATE_TAG.finditer(text):
        if any(a <= match.start() < b for a, b in code):
            continue
        closing = match.group().startswith("</")
        if depth == 0:
            parts.append(text[start:match.start()])
        if closing:
            depth = max(0, depth - 1)
            if depth == 0:
                start = match.end()
        else:
            if depth == 0 and parts and parts[-1] and not parts[-1].endswith("\n"):
                parts.append("\n")
            depth += 1
    if depth == 0:
        parts.append(text[start:])
    return "".join(parts)
