"""The U+00B7 separator must appear in code only as an escape, never literally.

`/usage` separates its fields with U+00B7, so that character is load-bearing in
`LINE_RE` and in every fixture that imitates real output. A literal in the
source is a live hazard: anything that reads the file as cp1252 and writes it
back as UTF-8 double-encodes it silently. A PowerShell
`Get-Content | Set-Content` round-trip does exactly that, and did -- it turned
the separator inside `LINE_RE` into the same mojibake the encoding bug produced
from the other direction. The file still imports and still looks right in a
terminal; only the bytes differ.

Writing it as an escape keeps the code pure ASCII, so no such round-trip can
alter it at all. These tests enforce that, and this file is itself ASCII --
the character below is built with chr() rather than typed, so the guard cannot
become the thing it guards against.

Comments are exempt on purpose. The examples of real `/usage` output above
`LINE_RE` read better with the actual character, and corruption in a comment
cannot change behaviour.
"""

import io
import pathlib

from niceclaude import cli

MIDDLE_DOT = chr(0x00B7)          # the separator itself
MOJIBAKE = chr(0x00C2) + chr(0x00B7)   # what cp1252 makes of its UTF-8 bytes

REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCES = sorted(
    list((REPO / "src" / "niceclaude").glob("*.py"))
    + list((REPO / "tests").glob("*.py"))
)


def literal_dots_outside_comments(path):
    """(line number, text) for every non-comment line holding a literal dot."""
    found = []
    with io.open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if line.lstrip().startswith("#"):
                continue
            if MIDDLE_DOT in line:
                found.append((number, line.rstrip()))
    return found


def test_sources_were_found():
    """Guard the guard: a bad glob would make the scan below vacuously pass."""
    names = {p.name for p in SOURCES}
    assert {"cli.py", "plot.py", "test_niceclaude.py"} <= names
    assert len(SOURCES) >= 8


def test_source_has_no_literal_middle_dot():
    leaks = []
    for path in SOURCES:
        for number, text in literal_dots_outside_comments(path):
            leaks.append(f"{path.name}:{number}: {text.strip()[:70]}")
    assert not leaks, (
        "literal U+00B7 found outside a comment; write it as an escape "
        "instead so the file stays pure ASCII:\n  " + "\n  ".join(leaks)
    )


def test_line_re_pattern_is_pure_ascii():
    """The compiled pattern carries the escape, not the character."""
    offenders = [c for c in cli.LINE_RE.pattern if ord(c) >= 128]
    assert not offenders, f"non-ASCII in LINE_RE.pattern: {offenders}"


def test_escaped_pattern_still_matches_the_real_separator():
    """The escape has to be behaviourally identical to the literal it replaced,
    including still rejecting the double-encoded form."""
    real = f"Current session: 11% used {MIDDLE_DOT} resets Aug 14, 8:10pm (UTC)"
    moji = f"Current session: 11% used {MOJIBAKE} resets Aug 14, 8:10pm (UTC)"
    match = cli.LINE_RE.match(real)
    assert match is not None
    assert match.group("resets") == "Aug 14, 8:10pm"
    assert cli.LINE_RE.match(moji) is None
