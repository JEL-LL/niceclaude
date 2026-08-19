"""`niceclaude install` edits a file it does not own.

Claude Code's ~/.claude/settings.json holds the user's model, theme,
permissions and quite possibly their own hooks. Installing into it is what makes
`niceclaude on <folder>` sufficient with no special launch -- but it means every
one of these has to hold:

  * nothing already in the file is lost, at any nesting level
  * a second install updates rather than duplicates (two registrations double
    the per-call latency, and after the tool venv moves one of them is dead)
  * a file we cannot parse is refused, not overwritten
  * `uninstall` round-trips back to exactly what was there before

The last one is the real test of the other three: if the merge quietly mutates
anything it does not own, the round-trip will not close.
"""

import copy
import json
import os

import pytest

from niceclaude import cli

HOOK = "/home/me/.local/bin/niceclaude-hook"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Point install at a scratch settings file and give it a findable hook."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "find_hook_exe", lambda: HOOK)
    return tmp_path / "settings.json"


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def commands(cfg, event="PreToolUse"):
    return [e.get("command")
            for g in cfg["hooks"][event] for e in g.get("hooks", [])]


# --- the file we do not own ---------------------------------------------------

def test_install_creates_the_file_when_absent(settings):
    assert cli.cmd_install(force=False) == 0
    cfg = read(settings)
    for event in cli.HOOK_EVENTS:
        assert commands(cfg, event) == [HOOK]


def test_install_preserves_every_unrelated_key(settings):
    original = {
        "model": "sonnet",
        "theme": "auto",
        "agentPushNotifEnabled": True,
        "permissions": {"allow": ["Bash(git diff:*)"], "deny": []},
        "env": {"FOO": "bar"},
    }
    settings.write_text(json.dumps(original), encoding="utf-8")

    assert cli.cmd_install(force=False) == 0
    cfg = read(settings)
    for key, value in original.items():
        assert cfg[key] == value, key
    assert commands(cfg) == [HOOK]


def test_install_keeps_someone_elses_hook_on_the_same_event(settings):
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [
            {"type": "command", "command": "/usr/local/bin/audit-bash"}]}]}}),
        encoding="utf-8")

    assert cli.cmd_install(force=False) == 0
    cfg = read(settings)
    assert commands(cfg) == ["/usr/local/bin/audit-bash", HOOK]


def test_install_does_not_disturb_an_unrelated_event(settings):
    settings.write_text(json.dumps({"hooks": {"Stop": [
        {"matcher": "*", "hooks": [
            {"type": "command", "command": "notify-send done"}]}]}}),
        encoding="utf-8")

    assert cli.cmd_install(force=False) == 0
    cfg = read(settings)
    assert commands(cfg, "Stop") == ["notify-send done"]
    assert commands(cfg) == [HOOK]


# --- idempotency and repair ---------------------------------------------------

def test_second_install_does_not_duplicate(settings):
    assert cli.cmd_install(force=False) == 0
    first = read(settings)
    assert cli.cmd_install(force=False) == 0
    assert read(settings) == first


def test_install_updates_a_moved_hook_path_in_place(settings, monkeypatch):
    """The failure this guards against is subtle: append instead of update and
    the file carries a live entry plus a dead one, so it reads as correct while
    every tool call pays the latency twice."""
    assert cli.cmd_install(force=False) == 0
    moved = "/opt/venvs/nc/bin/niceclaude-hook"
    monkeypatch.setattr(cli, "find_hook_exe", lambda: moved)

    assert cli.cmd_install(force=False) == 0
    assert commands(read(settings)) == [moved]


def test_install_updates_without_widening_a_narrowed_matcher(settings, monkeypatch):
    """If someone has deliberately scoped our hook to Bash only, a reinstall
    fixes the command and leaves their scoping decision alone."""
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [
            {"type": "command", "command": "/old/bin/niceclaude-hook"}]}]}}),
        encoding="utf-8")

    assert cli.cmd_install(force=False) == 0
    cfg = read(settings)
    assert cfg["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert commands(cfg) == [HOOK]


def test_a_quoted_spaced_path_is_recognized_as_ours(settings, monkeypatch):
    monkeypatch.setattr(cli, "find_hook_exe", lambda: "/opt/my tools/niceclaude-hook")
    assert cli.cmd_install(force=False) == 0
    assert cli.cmd_install(force=False) == 0
    assert commands(read(settings)) == ['"/opt/my tools/niceclaude-hook"']


# --- refusing to clobber ------------------------------------------------------

def test_unparseable_settings_are_refused_not_overwritten(settings, capsys):
    broken = '{"model": "sonnet",,,}'
    settings.write_text(broken, encoding="utf-8")

    assert cli.cmd_install(force=False) == 1
    assert settings.read_text(encoding="utf-8") == broken
    assert "could not read" in capsys.readouterr().err


def test_a_non_object_settings_file_is_refused(settings, capsys):
    settings.write_text('["not", "a", "mapping"]', encoding="utf-8")
    assert cli.cmd_install(force=False) == 1
    assert "JSON object" in capsys.readouterr().err


def test_a_hooks_key_of_the_wrong_shape_is_refused(settings, capsys):
    settings.write_text('{"hooks": "surprise"}', encoding="utf-8")
    assert cli.cmd_install(force=False) == 1
    assert "not an object" in capsys.readouterr().err
    assert read(settings) == {"hooks": "surprise"}


def test_an_event_of_the_wrong_shape_is_refused(settings, capsys):
    settings.write_text('{"hooks": {"PreToolUse": {"matcher": "*"}}}',
                        encoding="utf-8")
    assert cli.cmd_install(force=False) == 1
    assert "not an array" in capsys.readouterr().err


def test_install_fails_cleanly_when_the_hook_executable_is_missing(
        settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_hook_exe", lambda: None)
    assert cli.cmd_install(force=False) == 1
    assert not settings.exists()
    assert "could not find" in capsys.readouterr().err


# --- the round trip -----------------------------------------------------------

@pytest.mark.parametrize("original", [
    {},
    {"model": "sonnet", "theme": "auto"},
    {"permissions": {"allow": ["Bash(git diff:*)"]}, "env": {"FOO": "bar"}},
    {"hooks": {"Stop": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "notify-send done"}]}]}},
    {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "/usr/local/bin/audit"}]}]}},
    {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "/usr/local/bin/audit"},
        {"type": "command", "command": "/usr/local/bin/tally"}]}]}},
])
def test_install_then_uninstall_restores_the_original_exactly(settings, original):
    """The strongest statement available: whatever we found, we put back.

    This is the real test of the merge. Any stray mutation at any depth -- a
    reordered list, a dropped sibling, a rewritten matcher -- fails here even if
    every assertion above happens to pass.
    """
    settings.write_text(json.dumps(original), encoding="utf-8")
    before = copy.deepcopy(original)

    assert cli.cmd_install(force=False) == 0
    assert cli.cmd_uninstall() == 0
    assert read(settings) == before


@pytest.mark.parametrize("original,expected", [
    ({"hooks": {}}, {}),
    ({"model": "opus", "hooks": {"PreToolUse": []}}, {"model": "opus"}),
    ({"hooks": {"PreToolUse": [], "Stop": []}}, {"hooks": {"Stop": []}}),
])
def test_an_empty_hook_container_is_normalized_away_by_the_round_trip(
        settings, original, expected):
    """The one thing the round trip does not preserve, stated explicitly.

    `uninstall` cannot distinguish a container it emptied from one that was
    already empty, so it prunes on emptiness. That loses nothing: an empty hook
    list and an absent key mean the same thing to Claude Code. Note the third
    case -- an empty event we never touched is still left alone, because pruning
    is decided per event rather than from a single global "did anything change".
    """
    settings.write_text(json.dumps(original), encoding="utf-8")
    assert cli.cmd_install(force=False) == 0
    assert cli.cmd_uninstall() == 0
    assert read(settings) == expected


def test_uninstall_is_quiet_and_harmless_when_nothing_is_registered(
        settings, capsys):
    settings.write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")
    assert cli.cmd_uninstall() == 0
    assert read(settings) == {"model": "sonnet"}
    assert "no niceclaude hook registered" in capsys.readouterr().out


def test_uninstall_leaves_a_neighbour_hook_in_the_same_group(settings):
    """Our entry can share a matcher group with someone else's. Removing ours
    must take the entry, not the group."""
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [
            {"type": "command", "command": "/usr/local/bin/audit"},
            {"type": "command", "command": HOOK}]}]}}),
        encoding="utf-8")

    assert cli.cmd_uninstall() == 0
    cfg = read(settings)
    assert commands(cfg) == ["/usr/local/bin/audit"]


def test_uninstall_removes_the_fragment_but_not_the_policy(settings):
    assert cli.cmd_install(force=False) == 0
    assert os.path.exists(cli.SETTINGS_PATH)
    assert cli.cmd_uninstall() == 0
    assert not os.path.exists(cli.SETTINGS_PATH)
    assert os.path.exists(cli.POLICY_PATH)


# --- CLAUDE_CONFIG_DIR --------------------------------------------------------

def test_claude_config_dir_is_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert cli.claude_settings_path() == str(tmp_path / "elsewhere" / "settings.json")


def test_default_location_is_dot_claude_under_home(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert cli.claude_settings_path() == os.path.join(cli.HOME, ".claude",
                                                     "settings.json")
