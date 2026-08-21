"""The version is stored in exactly one place, and the code can find it.

Two copies of a version number cannot be kept in agreement by hand. The one
check that already existed -- publish.yml comparing the pushed tag against
pyproject.toml -- covers that copy and no other, so a literal anywhere else
would drift silently and be believed. These tests assert the single-source
property directly, rather than trusting that nobody re-adds one.

The last test is the one that would have caught the bug that prompted all of
this: an installed build several commits behind the checkout beside it, which
reports itself as working because the version number never changed.
"""

import io
import pathlib
import subprocess
import sys
import tomllib
from importlib.metadata import version as metadata_version

from niceclaude import cli

REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCES = sorted((REPO / "src" / "niceclaude").glob("*.py"))


def pyproject_version():
    with io.open(REPO / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


# --- reading it ---------------------------------------------------------------

def test_installed_version_reports_the_distribution_metadata():
    assert cli.installed_version() == metadata_version("niceclaude")


def test_version_command_prints_it_and_succeeds(capsys):
    assert cli.cmd_version() == 0
    assert capsys.readouterr().out.strip() == f"niceclaude {pyproject_version()}"


def test_a_missing_distribution_is_reported_not_guessed(monkeypatch, capsys):
    """Running from a source tree with nothing installed.

    The temptation is to fall back to a hardcoded number, which would make the
    command confidently wrong in exactly the situation it exists to diagnose.
    """
    monkeypatch.setattr(cli, "installed_version", lambda: None)
    assert cli.cmd_version() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no installed distribution" in captured.err


# --- the single source -------------------------------------------------------

def test_no_source_file_carries_a_version_literal():
    """`__version__ = "..."` is what this design removed. Keep it removed.

    Matched on the assignment rather than the word, so the docstring in
    __init__.py explaining why the literal is absent does not trip the guard.
    """
    offenders = []
    for path in SOURCES:
        for number, line in enumerate(io.open(path, encoding="utf-8"), 1):
            stripped = line.strip()
            if stripped.startswith("__version__") and "=" in stripped:
                offenders.append(f"{path.name}:{number}: {stripped[:60]}")
    assert not offenders, (
        "the version is stored in pyproject.toml only; read it with "
        "cli.installed_version() instead of re-adding a literal:\n  "
        + "\n  ".join(offenders))


def test_sources_were_found():
    """Guard the guard: a bad glob makes the scan above vacuously pass."""
    assert {"cli.py", "hook.py", "__init__.py"} <= {p.name for p in SOURCES}


def test_the_hot_path_does_not_pay_for_the_lookup():
    """importlib.metadata walks sys.path for dist-info, which is far too
    expensive to sit in front of every tool call. It must not be imported until
    something actually asks for the version -- and the hook never does."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, niceclaude.hook; "
         "print('importlib.metadata' in sys.modules)"],
        capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False", out.stderr


# --- staleness ---------------------------------------------------------------

def test_the_installed_build_agrees_with_this_checkout():
    """The declared version and the installed one must match.

    A mismatch means the tests are being run against a build that is not this
    source tree, which makes every other result in the suite untrustworthy.
    """
    assert metadata_version("niceclaude") == pyproject_version()


def test_the_changelog_describes_the_declared_version():
    """The pyproject comment asks for a changelog entry on every bump. Nothing
    can verify that the entry is *accurate*, but an absent one is checkable."""
    changelog = REPO / "CHANGELOG.md"
    assert changelog.exists(), "CHANGELOG.md is missing"
    text = io.open(changelog, encoding="utf-8").read()
    declared = pyproject_version()
    assert f"## {declared}" in text, (
        f"pyproject.toml declares {declared} but CHANGELOG.md has no "
        f"'## {declared}' section")
