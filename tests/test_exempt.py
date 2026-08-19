"""NICECLAUDE_OFF, and status telling the truth about the plumbing.

These two exist because the hook is now installed globally. That removes the
need to launch Claude Code in a particular way, and in exchange it takes on two
obligations:

  * a single session must be able to opt out, since settings scopes cannot do
    it -- hooks merge additively and a narrower scope cannot un-register a
    broader one, so the exemption has to live outside settings entirely
  * `status` must never report a folder as paced when nothing is positioned to
    pace it, in either direction: no hook registered, or an exemption in force
"""

import io
import json

import pytest

from niceclaude import cli, hook


PAYLOAD = json.dumps({"cwd": "/tmp/nc-exempt-test",
                      "hook_event_name": "PreToolUse"})


@pytest.fixture
def paced_everything(tmp_path, monkeypatch):
    """A policy that would brake anything, so a pass-through proves an exemption."""
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "global": {"enabled": True},
        "paths": {"/": {"paced": True, "model": "opus", "m0": 0, "m1": 99}},
    }), encoding="utf-8")
    monkeypatch.setattr(hook, "POLICY_PATH", str(policy))
    return policy


def test_niceclaude_off_returns_immediately(paced_everything, monkeypatch):
    """The policy would brake; the exemption must win before any of it runs."""
    monkeypatch.setenv("NICECLAUDE_OFF", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(PAYLOAD))
    monkeypatch.setattr(hook, "run", _explode)
    assert hook.main() == 0


def test_any_non_empty_value_exempts(paced_everything, monkeypatch):
    """NICECLAUDE_OFF=0 meaning "pacing on" would be a trap. Anything set
    exempts; only unset or empty does not."""
    monkeypatch.setenv("NICECLAUDE_OFF", "0")
    monkeypatch.setattr("sys.stdin", io.StringIO(PAYLOAD))
    monkeypatch.setattr(hook, "run", _explode)
    assert hook.main() == 0


def test_empty_value_does_not_exempt(paced_everything, monkeypatch):
    monkeypatch.setenv("NICECLAUDE_OFF", "")
    monkeypatch.setattr("sys.stdin", io.StringIO(PAYLOAD))
    seen = []

    def fake_run(cwd, event=None):
        seen.append((cwd, event))
        return None, "unpaced"

    monkeypatch.setattr(hook, "run", fake_run)
    assert hook.main() == 0
    assert seen == [("/tmp/nc-exempt-test", "PreToolUse")]


def test_the_exemption_still_drains_stdin(paced_everything, monkeypatch):
    """Returning before the read would risk EPIPE on the caller's write."""
    stream = io.StringIO(PAYLOAD)
    monkeypatch.setenv("NICECLAUDE_OFF", "1")
    monkeypatch.setattr("sys.stdin", stream)
    monkeypatch.setattr(hook, "run", _explode)
    assert hook.main() == 0
    assert stream.tell() == len(PAYLOAD)


def _explode(*_args, **_kwargs):
    raise AssertionError("the exemption did not short-circuit the hook")


# --- status reports the plumbing, not just the policy -------------------------

def test_status_says_not_registered_when_nothing_is_installed(
        tmp_path, monkeypatch):
    """A folder reported as paced while no hook exists anywhere is the worst
    failure this tool has, and it is what sent a real user down this path."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(cli, "SETTINGS_PATH", str(tmp_path / "fragment.json"))
    assert "NOT REGISTERED" in cli.describe_installation()


def test_status_reports_a_global_install(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "find_hook_exe",
                        lambda: "/home/me/.local/bin/niceclaude-hook")
    assert cli.cmd_install(force=False) == 0
    assert "registered in" in cli.describe_installation()


def test_status_distinguishes_a_fragment_only_install(tmp_path, monkeypatch):
    """The old shape: the fragment exists but nothing is global, so pacing only
    reaches sessions launched with --settings. Saying "registered" here would be
    the same lie in a quieter voice."""
    fragment = tmp_path / "fragment.json"
    fragment.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [
            {"type": "command", "command": "/x/niceclaude-hook"}]}]}}),
        encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(cli, "SETTINGS_PATH", str(fragment))
    assert "fragment only" in cli.describe_installation()


def test_status_surfaces_an_active_exemption(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NICECLAUDE_OFF", "1")
    monkeypatch.setattr(cli, "find_hook_exe",
                        lambda: "/home/me/.local/bin/niceclaude-hook")
    assert cli.cmd_install(force=False) == 0
    assert "EXEMPT" in cli.describe_installation()
