"""Strict Python code-block detection.

This module is the single source of truth for deciding whether an incoming
Discord message contains runnable Python. It is intentionally conservative:

* Only fenced Markdown code blocks are considered.
* Only the language identifiers ``python`` and ``py`` are accepted
  (optionally the empty/unmarked identifier, when explicitly allowed).
* Ordinary prose is NEVER interpreted as code.
* ``None`` is returned whenever no supported block exists.

The functions here are pure (no I/O, no config imports) so they are trivial to
unit-test and extremely cheap to call on every message.
"""

from __future__ import annotations

import re
from typing import Iterator, List, Optional, Tuple

# Language identifiers we accept on the opening fence line.
_ALLOWED_LANGS = frozenset({"python", "py"})
_UNMARKED = ""

# A fenced code block:
#   ```<lang>\n<code>```
# - "lang": the text after the opening ``` on the same line (may be empty).
#   It cannot contain a newline or a backtick.
# - "code": everything up to the next ``` or the end of the string (DOTALL).
# Non-greedy + finditer yields blocks left-to-right, non-overlapping.
_FENCE_RE = re.compile(r"```(?P<lang>[^\n`]*)\n(?P<code>.*?)(?:```|\Z)", re.DOTALL)


def _normalize(content: str) -> str:
    """Normalize line endings so the regex behaves consistently."""
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _iter_blocks(content: str) -> Iterator[Tuple[str, str]]:
    """Yield (normalized_lang, raw_code) for every fenced block in order."""
    for match in _FENCE_RE.finditer(content):
        lang = match.group("lang").strip().lower()
        yield lang, match.group("code")


def _is_supported(lang: str, allow_unmarked: bool) -> bool:
    if lang in _ALLOWED_LANGS:
        return True
    return allow_unmarked and lang == _UNMARKED


def _clean(code: str) -> str:
    """Trim surrounding blank lines/whitespace while preserving indentation."""
    # Strip only newlines from the front (keep meaningful leading spaces of the
    # first statement is impossible at top level anyway), then trailing space.
    return code.strip("\n").rstrip()


def extract_python_code(
    message_content: Optional[str], *, allow_unmarked: bool = False
) -> Optional[str]:
    """Return the code of the FIRST supported Python block, else ``None``.

    Parameters
    ----------
    message_content:
        The raw Discord message content.
    allow_unmarked:
        When True, a bare ```` ``` ```` block with no language identifier is
        also treated as Python. Defaults to False (strict mode).
    """
    if not message_content:
        return None
    # Ultra-cheap early-out: the vast majority of chat messages have no fence.
    if "```" not in message_content:
        return None

    content = _normalize(message_content)
    for lang, raw_code in _iter_blocks(content):
        if _is_supported(lang, allow_unmarked):
            cleaned = _clean(raw_code)
            if cleaned:
                return cleaned
    return None


def extract_all_python_code(
    message_content: Optional[str], *, allow_unmarked: bool = False
) -> List[str]:
    """Return the code of EVERY supported Python block (in order).

    Used only when ``EXECUTE_ALL_BLOCKS`` is enabled. Returns an empty list when
    no supported block exists.
    """
    if not message_content or "```" not in message_content:
        return []

    content = _normalize(message_content)
    blocks: List[str] = []
    for lang, raw_code in _iter_blocks(content):
        if _is_supported(lang, allow_unmarked):
            cleaned = _clean(raw_code)
            if cleaned:
                blocks.append(cleaned)
    return blocks


def has_python_code_block(
    message_content: Optional[str], *, allow_unmarked: bool = False
) -> bool:
    """Cheap boolean check used for fast filtering."""
    return extract_python_code(message_content, allow_unmarked=allow_unmarked) is not None
