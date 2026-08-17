"""Regressions from the first run of this tool on Windows.

Three bugs surfaced the first time a line of this executed on Windows. None of
them could appear on Linux, and two failed *silently* in the one direction that
matters -- a folder that looks paced while nothing is governing it.

  1. `niceclaude install` wrote the hook command as a native Windows path.
     Claude Code runs hook commands through Git Bash, where backslash is an
     escape character, so the path was mangled and the hook never ran.
  2. `/usage` was decoded with the locale encoding rather than UTF-8, so the
     U+00B7 separator arrived as "Â·" and the two most important buckets
     stopped parsing.
  3. The reset clause was stamped UTC regardless of the zone printed beside it.
     Correct only on a machine set to UTC; four hours wrong on a workstation,
     in the fail-open direction.

See harness/windows-results.md for the evidence behind each.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from niceclaude import cli

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


def installed_command():
    with open(cli.SETTINGS_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)
    return cfg["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


# --- 1. the hook command has to survive a POSIX shell -------------------------

def test_install_command_carries_no_backslashes_on_windows(monkeypatch):
    """A backslash path is silently eaten by Git Bash and the hook never fires.

    This is the worst failure this tool has: nothing errors, nothing logs, and
    the folder reports itself paced while every tool call sails through.
    """
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli, "find_hook_exe",
                        lambda: r"C:\Users\me\.local\bin\niceclaude-hook.exe")
    assert cli.cmd_install(force=False) == 0
    cmd = installed_command()
    assert "\\" not in cmd
    assert cmd == "C:/Users/me/.local/bin/niceclaude-hook.exe"


def test_install_quotes_a_spaced_path_and_still_forward_slashes_it(monkeypatch):
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli, "find_hook_exe",
                        lambda: r"C:\Program Files\nc\niceclaude-hook.exe")
    assert cli.cmd_install(force=False) == 0
    assert installed_command() == '"C:/Program Files/nc/niceclaude-hook.exe"'


def test_install_leaves_posix_paths_alone(monkeypatch):
    monkeypatch.setattr(cli.os, "name", "posix")
    monkeypatch.setattr(cli, "find_hook_exe",
                        lambda: "/home/me/.local/bin/niceclaude-hook")
    assert cli.cmd_install(force=False) == 0
    assert installed_command() == "/home/me/.local/bin/niceclaude-hook"


# --- 2. `/usage` is UTF-8, whatever the locale says ---------------------------

def test_usage_output_is_decoded_as_utf8(monkeypatch):
    """text=True alone uses locale.getencoding(), which is cp1252 on a US
    Windows install. The decode has to be pinned to UTF-8 explicitly."""
    seen = {}

    class Proc:
        stdout = "Current session: 5% used \u00b7 resets Aug 14, 8:10pm (UTC)"
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return Proc()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    rec = cli.sample_once()
    assert seen.get("encoding") == "utf-8"
    assert rec["parse_ok"] is True
    assert rec["buckets"]["session"]["pct"] == 5.0


def test_mis_decoded_separator_is_surfaced_not_silently_dropped():
    """If the decode is ever wrong again, it must show up as an unparsed line
    rather than a quietly missing bucket -- `niceclaude check` is the only
    thing that would catch it."""
    buckets, unparsed, _info = cli.parse_usage(
        "Current session: 5% used \u00c2\u00b7 resets Aug 14, 8:10pm (UTC)", NOW)
    assert "session" not in buckets
    assert len(unparsed) == 1


# --- 3. the reset clause is in the zone printed beside it ---------------------

def test_iana_timezone_label_parses():
    """`[A-Z]{2,5}` could not match "America/New_York", so the optional reset
    group failed, so the whole line failed, so the session and
    week:all-models buckets vanished from the snapshot entirely."""
    buckets, unparsed, _info = cli.parse_usage(
        "Current session: 9% used \u00b7 resets Aug 14, 2:59pm (America/New_York)\n"
        "Current week (all models): 2% used \u00b7 resets Aug 16, 7:59pm (America/New_York)",
        NOW)
    assert unparsed == []
    assert buckets["session"]["tz"] == "America/New_York"
    assert buckets["session"]["pct"] == 9.0
    assert buckets["week:all models"]["tz"] == "America/New_York"


def test_utc_label_is_still_read_as_utc():
    assert cli.parse_reset("Aug 14, 8:10pm", NOW, "UTC") == \
        datetime(2026, 8, 14, 20, 10, tzinfo=timezone.utc)


def test_reset_is_converted_from_the_named_zone(monkeypatch):
    monkeypatch.setattr(cli, "resolve_tz",
                        lambda label: timezone(timedelta(hours=-4)))
    assert cli.parse_reset("Aug 14, 8:10pm", NOW, "America/New_York") == \
        datetime(2026, 8, 15, 0, 10, tzinfo=timezone.utc)


@pytest.mark.skipif(cli.resolve_tz("America/New_York") is None,
                    reason="no tz database on this platform (Windows ships none)")
def test_iana_names_resolve_where_a_tz_database_exists():
    """Linux and macOS have zoneinfo data, so the conversion is exact there."""
    assert cli.parse_reset("Aug 14, 8:10pm", NOW, "America/New_York") == \
        datetime(2026, 8, 15, 0, 10, tzinfo=timezone.utc)   # EDT is UTC-4


def test_unresolvable_zone_is_read_as_local_time_not_utc(monkeypatch):
    """Windows has no tz database, so the IANA name cannot be resolved. The
    renderer prints the machine's *own* zone, so local is the correct reading;
    stamping UTC put every reset hours early, which inflates f_t, raises the
    pace line, and permits spending that should have been braked."""
    monkeypatch.setattr(cli, "resolve_tz", lambda label: None)
    got = cli.parse_reset("Aug 14, 8:10pm", NOW, "Some/Unresolvable")
    assert got == datetime(2026, 8, 14, 20, 10).astimezone(timezone.utc)
