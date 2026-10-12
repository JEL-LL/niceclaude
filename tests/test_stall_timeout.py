"""`install` raises CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS, and takes back only
its own write.

Claude Code kills a background subagent after 10 minutes without progress, and
a hold in the SubagentStart hook is not progress, so without this a subagent
held longer than that is killed rather than paced. Unlike the subagent TTL it
is set by default. Like it, a value the user chose is never replaced or
removed: only one that matches the marker is niceclaude's.
"""

import json

import pytest

from niceclaude import cli
from niceclaude._shared import HOOK_TIMEOUT

HOOK = "/home/me/.local/bin/niceclaude-hook"
KEY = cli.STALL_ENV_KEY
OURS = cli.STALL_TIMEOUT_MS


@pytest.fixture
def settings(tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    claude.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.delenv(KEY, raising=False)
    monkeypatch.setattr(cli, "find_hook_exe", lambda: HOOK)
    monkeypatch.setattr(cli, "POLICY_PATH", str(tmp_path / "data" / "policy.json"))
    monkeypatch.setattr(cli, "SETTINGS_PATH",
                        str(tmp_path / "config" / "settings.json"))
    return claude / "settings.json"


@pytest.fixture
def marker():
    return cli.STALL_MARKER_PATH


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")


def recorded(target):
    return cli.load_stall_marker().get(cli._marker_key(str(target)))


def test_the_value_outlasts_the_longest_hold_by_the_default():
    assert int(OURS) == HOOK_TIMEOUT * 1000 + 600000
    assert isinstance(OURS, str)


def test_install_sets_it_and_records_it(settings):
    assert cli.main(["install"]) == 0
    assert read(settings)["env"] == {KEY: OURS}
    assert recorded(settings) == OURS


def test_install_keeps_the_rest_of_the_env_block(settings):
    write(settings, {"env": {"FOO": "bar"}})
    assert cli.main(["install"]) == 0
    assert read(settings)["env"] == {"FOO": "bar", KEY: OURS}


def test_rerunning_install_upgrades_an_older_install(settings):
    """An install from before this existed has the hook but no key."""
    assert cli.main(["install"]) == 0
    cfg = read(settings)
    del cfg["env"]
    write(settings, cfg)
    cli.save_stall_marker({})
    assert cli.main(["install"]) == 0
    assert read(settings)["env"][KEY] == OURS
    assert recorded(settings) == OURS


def test_a_value_of_our_own_from_an_earlier_release_is_replaced(settings):
    write(settings, {"env": {KEY: "1000"}})
    cli.save_stall_marker({cli._marker_key(str(settings)): "1000"})
    assert cli.main(["install"]) == 0
    assert read(settings)["env"][KEY] == OURS
    assert recorded(settings) == OURS


def test_a_long_value_of_the_users_is_left_alone(settings, capsys):
    mine = str(10 ** 12)
    write(settings, {"env": {KEY: mine}})
    assert cli.main(["install"]) == 0
    assert read(settings)["env"][KEY] == mine
    assert recorded(settings) is None
    assert "yours" in capsys.readouterr().out


def test_a_short_value_of_the_users_is_left_but_warned(settings, capsys):
    write(settings, {"env": {KEY: "600000"}})
    assert cli.main(["install"]) == 0
    assert read(settings)["env"][KEY] == "600000"
    assert recorded(settings) is None
    assert "killed as stalled" in capsys.readouterr().out


def test_an_env_that_is_not_an_object_does_not_fail_install(settings, capsys):
    write(settings, {"env": ["nope"]})
    assert cli.main(["install"]) == 0
    cfg = read(settings)
    assert cfg["env"] == ["nope"]
    assert cfg["hooks"]
    assert "not an object" in capsys.readouterr().out


def test_install_does_not_claim_a_value_the_user_already_had(settings):
    write(settings, {"env": {KEY: OURS}})
    assert cli.main(["install"]) == 0
    assert recorded(settings) is None
    assert cli.main(["uninstall"]) == 0
    assert read(settings)["env"] == {KEY: OURS}


def test_uninstall_round_trips_an_absent_env_block(settings, marker):
    write(settings, {"model": "opus"})
    assert cli.main(["install"]) == 0
    assert cli.main(["uninstall"]) == 0
    assert read(settings) == {"model": "opus"}
    assert recorded(settings) is None


def test_uninstall_keeps_the_users_other_env_keys(settings):
    write(settings, {"env": {"FOO": "bar"}})
    assert cli.main(["install"]) == 0
    assert cli.main(["uninstall"]) == 0
    assert read(settings)["env"] == {"FOO": "bar"}


def test_uninstall_leaves_a_value_changed_after_install(settings, capsys):
    assert cli.main(["install"]) == 0
    cfg = read(settings)
    cfg["env"][KEY] = "999"
    write(settings, cfg)
    assert cli.main(["uninstall"]) == 0
    assert read(settings)["env"][KEY] == "999"
    assert recorded(settings) is None
    assert "not the" in capsys.readouterr().out


def test_two_config_dirs_keep_separate_records(settings, tmp_path, monkeypatch):
    assert cli.main(["install"]) == 0
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    assert cli.main(["install"]) == 0
    assert cli.main(["uninstall"]) == 0
    assert KEY not in read(other / "settings.json").get("env", {})
    assert recorded(settings) == OURS


def test_the_key_stays_out_of_the_settings_fragment(settings):
    assert cli.main(["install"]) == 0
    assert "env" not in read(cli.SETTINGS_PATH)


# --- status ------------------------------------------------------------------

def test_status_is_quiet_after_install(settings):
    assert cli.main(["install"]) == 0
    assert cli.stall_timeout_problem(str(settings), env={}) is None
    assert "WARNING" not in cli.describe_installation()


def test_status_warns_when_unset(settings):
    write(settings, {})
    assert "unset" in cli.stall_timeout_problem(str(settings), env={})


def test_status_warns_on_a_short_value(settings):
    write(settings, {"env": {KEY: "600000"}})
    assert "600000" in cli.stall_timeout_problem(str(settings), env={})


def test_status_accepts_a_long_enough_shell_value(settings):
    write(settings, {})
    assert cli.stall_timeout_problem(str(settings), env={KEY: OURS}) is None


def test_the_settings_value_beats_the_shell(settings):
    write(settings, {"env": {KEY: "600000"}})
    assert cli.stall_timeout_problem(str(settings), env={KEY: OURS})


def test_describe_installation_carries_the_warning(settings):
    assert cli.main(["install"]) == 0
    cfg = read(settings)
    del cfg["env"]
    write(settings, cfg)
    assert "WARNING" in cli.describe_installation()
