"""--subagent-cache-1h / -5m, and the note `on` prints without them.

Subagents (and workflows, teammates, compaction) get a 5-minute prompt cache
by default, even on a subscription; since Claude Code v2.1.242 the
`subagentPromptCacheTtl` setting can raise it to 1h. A hold longer than 5
minutes may wake a subagent
cold, and 1h cache writes bill higher, so niceclaude sets the key only when
asked -- and, having set it, must be able to take back exactly its own write
and never the user's. Most of what is pinned here is that second half: a key
the user set, or changed after us, survives `uninstall` and `off`.

Every path is redirected: Claude's config dir, the data-dir marker and the
policy all live under tmp_path, and the TTL environment variables are cleared
so a developer's shell cannot change the verdicts.
"""

import json
import time
from types import SimpleNamespace
import os

import pytest

from niceclaude import cli
from niceclaude._shared import norm_path

HOOK = "/home/me/.local/bin/niceclaude-hook"
KEY = cli.SUBAGENT_TTL_KEY
ENV = ("FORCE_PROMPT_CACHING_5M", "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL",
       "ENABLE_PROMPT_CACHING_1H")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Everything niceclaude reads or writes, moved under tmp_path."""
    claude = tmp_path / "claude"
    claude.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setattr(cli, "find_hook_exe", lambda: HOOK)
    monkeypatch.setattr(cli, "CLAUDE_SETTINGS_MARKER_PATH",
                        str(tmp_path / "data" / "claude_settings_marker.json"))
    monkeypatch.setattr(cli, "POLICY_PATH", str(tmp_path / "data" / "policy.json"))
    monkeypatch.setattr(cli, "SETTINGS_PATH",
                        str(tmp_path / "config" / "settings.json"))
    # `status` reads the snapshot; never the developer's real one.
    monkeypatch.setattr(cli, "STATE_PATH", str(tmp_path / "data" / "state.json"))
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    # `on` asks git whether the file it wrote is ignored. Answer "not a
    # repository" unless a test says otherwise, so no test depends on whether
    # git is installed or on what repository the temp dir happens to sit in.
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(returncode=128))
    # Nor on whether the temp dir sits inside a git checkout: no walk up for
    # a repository root unless a test asks for one with the `unix` fixture.
    monkeypatch.setattr(cli, "_local_settings_follow_repo", lambda: False)
    return tmp_path


@pytest.fixture
def user_settings(env):
    return env / "claude" / "settings.json"


@pytest.fixture
def marker(env):
    return env / "data" / "claude_settings_marker.json"


@pytest.fixture
def folder(env):
    d = env / "work"
    d.mkdir()
    return d


def local_settings(folder):
    return folder / ".claude" / "settings.local.json"


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj),
                    encoding="utf-8")


def on(folder, max_delay=None, no_max_delay=False, ttl=None):
    return cli.cmd_on(str(folder), model="opus", m0=None, m1=None,
                      fanout_reserve=None, enforce=None, max_delay=max_delay,
                      no_max_delay=no_max_delay, subagent_ttl=ttl)


def rule(folder):
    return read(cli.POLICY_PATH)["paths"][norm_path(str(folder))]


# --- install / uninstall -----------------------------------------------------

def test_install_without_a_flag_leaves_the_key_absent(env, user_settings, marker):
    assert cli.main(["install"]) == 0
    assert KEY not in read(user_settings)
    assert not marker.exists()


def test_install_without_a_flag_leaves_an_existing_value_alone(env, user_settings):
    write(user_settings, {KEY: "1h"})
    assert cli.main(["install"]) == 0
    assert read(user_settings)[KEY] == "1h"


@pytest.mark.parametrize("flag,value", [("--subagent-cache-1h", "1h"),
                                        ("--subagent-cache-5m", "5m")])
def test_install_flag_sets_the_key_and_writes_the_marker(env, user_settings,
                                                         marker, flag, value):
    assert cli.main(["install", flag]) == 0
    cfg = read(user_settings)
    assert cfg[KEY] == value
    assert cfg["hooks"]                        # the same write as the hook
    assert read(marker) == {cli._marker_key(str(user_settings)): value}


def test_the_ttl_stays_out_of_the_settings_fragment(env):
    """The fragment is the hook's registration for --settings; a user
    preference riding along would apply to every session launched with it."""
    assert cli.main(["install", "--subagent-cache-1h"]) == 0
    with open(cli.SETTINGS_PATH, encoding="utf-8") as fh:
        assert KEY not in fh.read()


def test_the_flags_are_mutually_exclusive(env, capsys):
    for cmd in (["install"], ["on", "x"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(cmd + ["--subagent-cache-1h", "--subagent-cache-5m"])
        assert exc.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_install_overwrites_an_existing_value_and_says_what_it_was(
        env, user_settings, marker, capsys):
    write(user_settings, {KEY: "5m", "model": "opus"})
    assert cli.main(["install", "--subagent-cache-1h"]) == 0
    cfg = read(user_settings)
    assert (cfg[KEY], cfg["model"]) == ("1h", "opus")
    assert '(was "5m")' in capsys.readouterr().out
    assert read(marker) == {cli._marker_key(str(user_settings)): "1h"}


def test_install_does_not_claim_a_value_that_was_already_there(
        env, user_settings, marker):
    """Else uninstall would take back a setting the user made themselves."""
    write(user_settings, {KEY: "1h"})
    assert cli.main(["install", "--subagent-cache-1h"]) == 0
    assert not marker.exists()
    assert cli.main(["uninstall"]) == 0
    assert read(user_settings)[KEY] == "1h"


def test_install_still_refuses_an_unparsable_settings_file(env, user_settings,
                                                           marker):
    write(user_settings, "{ not json")
    assert cli.main(["install", "--subagent-cache-1h"]) == 1
    assert user_settings.read_text(encoding="utf-8") == "{ not json"
    assert not marker.exists()


def test_uninstall_removes_the_key_when_the_marker_matches(
        env, user_settings, marker, capsys):
    write(user_settings, {"model": "opus"})
    assert cli.main(["install", "--subagent-cache-1h"]) == 0
    assert cli.main(["uninstall"]) == 0
    assert read(user_settings) == {"model": "opus"}
    assert not marker.exists()
    assert f"removed {KEY}" in capsys.readouterr().out


def test_uninstall_leaves_a_value_the_user_changed_afterwards(
        env, user_settings, marker, capsys):
    assert cli.main(["install", "--subagent-cache-1h"]) == 0
    cfg = read(user_settings)
    cfg[KEY] = "5m"
    write(user_settings, cfg)
    capsys.readouterr()

    assert cli.main(["uninstall"]) == 0
    assert read(user_settings)[KEY] == "5m"
    assert "not the 1h niceclaude wrote" in capsys.readouterr().out
    assert not marker.exists()


def test_uninstall_leaves_the_key_with_no_marker(env, user_settings, capsys):
    assert cli.main(["install"]) == 0
    cfg = read(user_settings)
    cfg[KEY] = "1h"
    write(user_settings, cfg)
    capsys.readouterr()

    assert cli.main(["uninstall"]) == 0
    assert read(user_settings) == {KEY: "1h"}
    assert "niceclaude did not set it" in capsys.readouterr().out


def test_a_marker_for_another_settings_file_is_not_ours(env, user_settings,
                                                        marker):
    other = cli._marker_key(str(env / "elsewhere" / "settings.json"))
    write(user_settings, {KEY: "1h"})
    write(marker, {other: "1h"})
    assert cli.main(["uninstall"]) == 0
    assert read(user_settings)[KEY] == "1h"
    assert read(marker) == {other: "1h"}      # the other entry survives


def test_the_old_single_record_marker_is_still_read(env, user_settings,
                                                    marker):
    write(user_settings, {KEY: "1h"})
    write(marker, {"settings": str(user_settings), KEY: "1h"})
    assert cli.main(["uninstall"]) == 0
    assert KEY not in read(user_settings)
    assert not marker.exists()


def test_installs_under_two_config_dirs_keep_separate_records(
        env, marker, monkeypatch):
    a, b = env / "claude", env / "claude-b"
    b.mkdir()
    assert cli.main(["install", "--subagent-cache-1h"]) == 0
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(b))
    assert cli.main(["install", "--subagent-cache-5m"]) == 0

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(a))
    assert cli.main(["uninstall"]) == 0
    assert KEY not in read(a / "settings.json")
    assert read(b / "settings.json")[KEY] == "5m"
    assert read(marker) == {cli._marker_key(str(b / "settings.json")): "5m"}

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(b))
    assert cli.main(["uninstall"]) == 0
    assert KEY not in read(b / "settings.json")
    assert not marker.exists()                # the map emptied, so it went


# --- on / off ----------------------------------------------------------------

def test_on_with_a_flag_writes_settings_local_preserving_other_keys(env, folder):
    write(local_settings(folder), {"permissions": {"allow": ["Bash(ls)"]}})
    assert on(folder, ttl="1h") == 0
    assert read(local_settings(folder)) == {
        "permissions": {"allow": ["Bash(ls)"]}, KEY: "1h"}
    assert rule(folder)[cli.SUBAGENT_TTL_RECORD] == "1h"


def test_on_with_a_flag_creates_dot_claude(env, folder):
    assert on(folder, max_delay=240, ttl="5m") == 0
    assert read(local_settings(folder)) == {KEY: "5m"}


def test_on_refuses_an_unparsable_settings_local(env, folder, capsys):
    write(local_settings(folder), "[1, 2")
    assert on(folder, ttl="1h") == 1
    assert local_settings(folder).read_text(encoding="utf-8") == "[1, 2"
    assert "Refusing" in capsys.readouterr().err
    # Nothing half-applied: the rule was not written either.
    assert norm_path(str(folder)) not in cli.load_policy()["paths"]


def test_on_refuses_a_settings_local_that_is_not_an_object(env, folder):
    write(local_settings(folder), "[]")
    assert on(folder, ttl="1h") == 1
    assert local_settings(folder).read_text(encoding="utf-8") == "[]"


def test_on_without_a_flag_never_writes(env, folder, user_settings):
    for max_delay in (None, 120, 600):
        assert on(folder, max_delay=max_delay) == 0
    assert not (folder / ".claude").exists()
    assert not user_settings.exists()
    assert cli.SUBAGENT_TTL_RECORD not in rule(folder)


def test_off_removes_only_its_own_value(env, folder, capsys):
    write(local_settings(folder), {"model": "sonnet"})
    assert on(folder, ttl="1h") == 0
    assert cli.cmd_off(str(folder)) == 0
    assert read(local_settings(folder)) == {"model": "sonnet"}
    assert cli.SUBAGENT_TTL_RECORD not in rule(folder)
    assert rule(folder)["paced"] is False


def test_off_keeps_the_file_even_when_it_empties(env, folder):
    assert on(folder, ttl="1h") == 0
    assert cli.cmd_off(str(folder)) == 0
    assert read(local_settings(folder)) == {}


def test_off_leaves_a_value_the_user_changed(env, folder, capsys):
    assert on(folder, ttl="1h") == 0
    write(local_settings(folder), {KEY: "5m"})
    assert cli.cmd_off(str(folder)) == 0
    assert read(local_settings(folder)) == {KEY: "5m"}
    assert "not the 1h niceclaude wrote" in capsys.readouterr().out


def test_off_without_a_record_leaves_the_users_value(env, folder):
    write(local_settings(folder), {KEY: "1h"})
    assert on(folder) == 0
    assert cli.cmd_off(str(folder)) == 0
    assert read(local_settings(folder)) == {KEY: "1h"}


def test_on_twice_with_the_same_flag_still_lets_off_release_it(env, folder):
    assert on(folder, ttl="1h") == 0
    assert on(folder, ttl="1h") == 0
    assert rule(folder)[cli.SUBAGENT_TTL_RECORD] == "1h"
    assert cli.cmd_off(str(folder)) == 0
    assert KEY not in read(local_settings(folder))


def test_a_second_flag_replaces_the_record(env, folder):
    assert on(folder, ttl="5m") == 0
    assert on(folder, ttl="1h") == 0
    assert rule(folder)[cli.SUBAGENT_TTL_RECORD] == "1h"
    assert cli.cmd_off(str(folder)) == 0
    assert KEY not in read(local_settings(folder))


def test_off_on_an_unreadable_file_keeps_the_record_to_retry(env, folder,
                                                            capsys):
    assert on(folder, ttl="1h") == 0
    local_settings(folder).write_text("{ broken", encoding="utf-8")
    capsys.readouterr()
    assert cli.cmd_off(str(folder)) == 0
    assert local_settings(folder).read_text(encoding="utf-8") == "{ broken"
    assert rule(folder)[cli.SUBAGENT_TTL_RECORD] == "1h"
    assert rule(folder)["paced"] is False
    assert f"left {KEY} alone" in capsys.readouterr().out

    write(local_settings(folder), {KEY: "1h", "model": "opus"})
    assert cli.cmd_off(str(folder)) == 0
    assert read(local_settings(folder)) == {"model": "opus"}
    assert cli.SUBAGENT_TTL_RECORD not in rule(folder)


def test_on_with_a_flag_refuses_a_missing_folder(env, capsys):
    missing = env / "nope"
    assert on(missing, ttl="1h") == 1
    assert not missing.exists()
    assert norm_path(str(missing)) not in cli.load_policy()["paths"]


def test_install_force_keeps_records_so_off_can_still_release(env, folder):
    assert on(folder, ttl="1h") == 0
    assert cli.main(["install", "--force"]) == 0
    pol = cli.load_policy()
    # Not a stub rule: that would shadow a later `on` of a parent folder.
    assert pol["paths"] == {}
    assert cli.cmd_off(str(folder)) == 0
    assert KEY not in read(local_settings(folder))
    assert cli.SUBAGENT_TTL_ORPHANS not in cli.load_policy()


@pytest.mark.parametrize("bad", ["[1]", "null", '{"paths": []}',
                                 '{"paths": ["a"]}', '{"paths": "x"}'])
def test_install_force_still_repairs_a_misshapen_policy(env, bad):
    os.makedirs(os.path.dirname(cli.POLICY_PATH), exist_ok=True)
    with open(cli.POLICY_PATH, "w", encoding="utf-8") as fh:
        fh.write(bad)
    assert cli.main(["install", "--force"]) == 0
    assert cli.load_policy() == cli.DEFAULT_POLICY


def test_a_second_install_force_keeps_the_orphaned_records(env, folder):
    assert on(folder, ttl="1h") == 0
    assert cli.main(["install", "--force"]) == 0
    assert cli.main(["install", "--force"]) == 0
    assert cli.load_policy()[cli.SUBAGENT_TTL_ORPHANS] == {
        norm_path(str(folder)): "1h"}
    assert cli.cmd_off(str(folder)) == 0
    assert KEY not in read(local_settings(folder))


def test_an_orphaned_record_does_not_unpace_a_parent_rule(env, folder,
                                                          capsys):
    from niceclaude import hook
    assert on(folder, ttl="1h") == 0
    assert cli.main(["install", "--force"]) == 0
    assert on(env) == 0                        # pace the parent
    pol = cli.load_policy()
    assert hook.paced_entry(pol, norm_path(str(folder))) is not None
    capsys.readouterr()
    assert cli.cmd_status(str(folder)) == 0
    out = capsys.readouterr().out
    assert f"matched rule:   {norm_path(str(env))}" in out
    assert "paced         True" in out
    # ...and `on` of the folder itself adopts the record back into its rule.
    assert on(folder) == 0
    assert rule(folder)[cli.SUBAGENT_TTL_RECORD] == "1h"
    assert cli.SUBAGENT_TTL_ORPHANS not in cli.load_policy()


def test_on_does_not_claim_a_value_already_there(env, folder):
    write(local_settings(folder), {KEY: "1h"})
    assert on(folder, ttl="1h") == 0
    assert cli.SUBAGENT_TTL_RECORD not in rule(folder)
    assert cli.cmd_off(str(folder)) == 0
    assert read(local_settings(folder)) == {KEY: "1h"}


# --- the advisory ------------------------------------------------------------

def note(capsys):
    """The TTL note from `on`'s output: its note: line and continuations."""
    out = capsys.readouterr().out.splitlines()
    for i, line in enumerate(out):
        if line.startswith("note:") and "max_delay" in line:
            rest = [l for l in out[i + 1:] if l.startswith("      ")]
            return "\n".join([line] + rest)
    return ""


def test_advisory_a_recommends_1h_when_holds_are_uncapped(env, folder, capsys):
    assert on(folder) == 0
    text = note(capsys)
    assert "--subagent-cache-1h" in text
    assert "may" in text and "mileage may vary" in text


def test_advisory_a_fires_for_a_cap_over_the_threshold(env, folder, capsys):
    assert on(folder, max_delay=cli.SUBAGENT_5M_SAFE_HOLD + 1) == 0
    assert "--subagent-cache-1h" in note(capsys)


def test_advisory_a_quiet_when_1h_is_already_in_force(env, folder, capsys,
                                                      user_settings):
    write(user_settings, {KEY: "1h"})
    assert on(folder) == 0
    assert note(capsys) == ""


def test_advisory_a_quiet_under_the_threshold_on_5m(env, folder, capsys):
    assert on(folder, max_delay=cli.SUBAGENT_5M_SAFE_HOLD) == 0
    assert note(capsys) == ""


def test_a_negative_max_delay_is_clamped_and_draws_no_advisory(env, folder,
                                                              capsys):
    assert on(folder, max_delay=-5) == 0
    assert note(capsys) == ""


def test_advisory_b_flags_1h_when_holds_stay_short(env, folder, capsys):
    write(local_settings(folder), {KEY: "1h"})
    assert on(folder, max_delay=240) == 0
    text = note(capsys)
    assert "likely extra cost" in text
    assert "--subagent-cache-5m" in text
    assert "mileage may vary" in text
    assert "--band" in text and "steps per wake" in text


def test_force_switch_beats_the_specific_variable_in_the_flag_note():
    """FORCE_PROMPT_CACHING_5M overrules CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL,
    so writing 5m under both is what takes effect -- no override warning."""
    both = {"FORCE_PROMPT_CACHING_5M": "1",
            "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": "1h"}
    assert cli.subagent_ttl_flag_note(60, "5m", env=both) is None
    assert "FORCE_PROMPT_CACHING_5M" in cli.subagent_ttl_flag_note(
        60, "1h", env=both)


def test_advisory_honours_a_defaults_level_max_delay(env, folder, capsys):
    pol = cli.load_policy()
    pol["defaults"]["max_delay"] = 200
    cli.save_policy(pol)
    assert on(folder) == 0
    assert note(capsys) == ""


def test_advisory_honours_an_explicit_no_max_delay(env, folder, capsys):
    pol = cli.load_policy()
    pol["defaults"]["max_delay"] = 200
    cli.save_policy(pol)
    assert on(folder, no_max_delay=True) == 0
    assert "--subagent-cache-1h" in note(capsys)


def test_force_5m_env_keeps_advisory_a_and_names_the_variable(
        env, folder, capsys, monkeypatch, user_settings):
    write(user_settings, {KEY: "1h"})
    monkeypatch.setenv("FORCE_PROMPT_CACHING_5M", "1")
    assert on(folder) == 0
    assert "FORCE_PROMPT_CACHING_5M" in note(capsys)


def test_env_ttl_1h_triggers_advisory_b(env, folder, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL", "1h")
    assert on(folder, max_delay=60) == 0
    assert "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL" in note(capsys)


def test_env_ttl_1h_quiets_advisory_a(env, folder, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL", "1h")
    assert on(folder) == 0
    assert note(capsys) == ""


def test_a_contradicting_flag_is_obeyed_with_one_short_note(env, folder,
                                                             capsys):
    assert on(folder, max_delay=60, ttl="1h") == 0
    assert read(local_settings(folder))[KEY] == "1h"
    out = capsys.readouterr().out
    notes = [l for l in out.splitlines()
             if l.startswith("note:") and not scope_note(l)]
    assert len(notes) == 1 and "worth testing" in notes[0]
    assert "mileage" not in out          # not the full advisory


def test_an_agreeing_flag_prints_no_note(env, folder, capsys):
    assert on(folder, max_delay=60, ttl="5m") == 0
    assert not [l for l in capsys.readouterr().out.splitlines()
                if l.startswith("note:") and not scope_note(l)]


def scope_note(line):
    """The two notes every TTL write may print, apart from the advice."""
    return "reaches sessions started in" in line or ".gitignore" in line


def local_path_of(folder):
    """The settings.local.json path exactly as `on` prints it: under the
    policy key, which is normcased on Windows."""
    return cli.folder_settings_paths(norm_path(str(folder)))[0]


@pytest.fixture
def unix(monkeypatch):
    """Take the non-Windows branch: settings.local.json follows the repo."""
    monkeypatch.setattr(cli, "_local_settings_follow_repo", lambda: True)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(cli, "_local_settings_follow_repo", lambda: False)


def make_repo(path):
    (path / ".git").mkdir(parents=True)
    return path


# --- which sessions the key reaches ------------------------------------------

@pytest.mark.parametrize("name,follows", [("nt", False), ("posix", True)])
def test_the_platform_check_is_windows_versus_the_rest(monkeypatch, name,
                                                       follows):
    monkeypatch.setattr(cli.os, "name", name)
    assert cli._local_settings_follow_repo() is follows


def test_repo_root_is_none_on_windows(env, folder, windows):
    make_repo(folder)
    assert cli.repo_local_settings_root(str(folder / "sub")) is None


def test_repo_root_is_found_by_walking_up(env, unix):
    repo = make_repo(env / "repo")
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert norm_path(cli.repo_local_settings_root(str(sub))) == norm_path(
        str(repo))


def test_repo_root_is_none_at_home(env, unix, monkeypatch):
    make_repo(env / "home")
    monkeypatch.setattr(cli, "HOME", str(env / "home"))
    (env / "home" / "proj").mkdir()
    assert cli.repo_local_settings_root(str(env / "home" / "proj")) is None


def test_a_worktree_resolves_to_the_main_checkout(env, unix):
    main = make_repo(env / "main")
    wt_git = main / ".git" / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    wt = env / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
    assert norm_path(cli.repo_local_settings_root(str(wt))) == norm_path(
        str(main))


def test_a_gitfile_we_cannot_follow_is_its_own_root(env, unix):
    sm = env / "submodule"
    sm.mkdir()
    (sm / ".git").write_text("gitdir: ../nowhere\n", encoding="utf-8")
    assert norm_path(cli.repo_local_settings_root(str(sm))) == norm_path(
        str(sm))


def test_scope_note_outside_a_repo(env, folder, unix, capsys, monkeypatch):
    # The walk itself is tested above; here the temp dir must not turn out to
    # be inside someone's checkout.
    monkeypatch.setattr(cli, "repo_local_settings_root", lambda folder: None)
    assert on(folder, ttl="1h") == 0
    out = capsys.readouterr().out
    assert f"note: reaches sessions started in {norm_path(str(folder))};" in out
    assert "repository" not in out and "precedence" not in out
    assert "install --subagent-cache-1h" in out


def test_scope_note_on_windows_ignores_the_repo(env, folder, windows, capsys):
    make_repo(folder)
    assert on(folder, ttl="1h") == 0
    out = capsys.readouterr().out
    assert f"note: reaches sessions started in {norm_path(str(folder))};" in out
    assert "repository" not in out


def test_scope_note_at_a_repo_root(env, folder, unix, capsys):
    make_repo(folder)
    assert on(folder, ttl="1h") == 0
    assert (f"note: reaches sessions started in {norm_path(str(folder))}, and "
            f"anywhere in the repository and its worktrees;"
            in capsys.readouterr().out)


def test_scope_note_inside_a_repo(env, unix, capsys):
    repo = make_repo(env / "repo")
    sub = repo / "pkg"
    sub.mkdir()
    assert on(sub, ttl="1h") == 0
    out = capsys.readouterr().out
    root_file = cli.folder_settings_paths(cli.repo_local_settings_root(
        norm_path(str(sub))))[0]
    assert (f"note: reaches sessions started in {norm_path(str(sub))}; a value "
            f"set in {root_file} takes precedence there;" in out)


def test_no_scope_notes_when_nothing_is_written(env, folder, capsys):
    write(local_settings(folder), {KEY: "1h"})
    assert on(folder, ttl="1h") == 0          # already set: no write
    out = capsys.readouterr().out
    assert "reaches sessions" not in out
    assert ".gitignore" not in out


# --- the gitignore note ------------------------------------------------------

class Ran:
    def __init__(self, returncode):
        self.returncode = returncode


def git_says(monkeypatch, outcome):
    """Make `git check-ignore` answer `outcome`: a return code, or an
    exception to raise. Records the calls so a test can check the question."""
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        if isinstance(outcome, BaseException):
            raise outcome
        return Ran(outcome)
    monkeypatch.setattr(cli.subprocess, "run", fake)
    return calls


def test_not_ignored_prints_the_gitignore_note(env, folder, monkeypatch,
                                               capsys):
    calls = git_says(monkeypatch, 1)
    assert on(folder, ttl="1h") == 0
    assert (f"note: {local_path_of(folder)} is not ignored by git; Claude "
            f"Code adds **/.claude/settings.local.json to your global git "
            f"excludes only the first time it writes the file itself, so add "
            f"it to .gitignore." in capsys.readouterr().out)
    assert calls == [["git", "-C", norm_path(str(folder)), "check-ignore",
                      "-q", local_path_of(folder)]]


@pytest.mark.parametrize("outcome", [0, 128, FileNotFoundError("git"),
                                     OSError("denied")])
def test_ignored_or_unknown_prints_nothing(env, folder, monkeypatch, capsys,
                                           outcome):
    git_says(monkeypatch, outcome)
    assert on(folder, ttl="1h") == 0
    assert ".gitignore" not in capsys.readouterr().out


def test_the_gitignore_check_follows_every_write(env, folder, monkeypatch,
                                                 capsys):
    """Not only a file `on` created: a rewrite can be just as unignored."""
    write(local_settings(folder), {"model": "opus"})
    git_says(monkeypatch, 1)
    assert on(folder, ttl="1h") == 0
    assert ".gitignore" in capsys.readouterr().out


def test_no_git_question_when_nothing_is_written(env, folder, monkeypatch):
    write(local_settings(folder), {KEY: "1h"})
    calls = git_says(monkeypatch, 1)
    assert on(folder, ttl="1h") == 0
    assert calls == []


# --- effective_subagent_ttl precedence ---------------------------------------

def test_default_is_5m(env, folder):
    assert cli.effective_subagent_ttl(str(folder), env={}) == ("5m", "default")


def test_precedence_order(env, folder, user_settings):
    f = str(folder)
    local, project = cli.folder_settings_paths(f)
    envs = {"ENABLE_PROMPT_CACHING_1H": "1"}
    assert cli.effective_subagent_ttl(f, envs) == ("1h",
                                                   "ENABLE_PROMPT_CACHING_1H")
    write(user_settings, {KEY: "5m"})
    assert cli.effective_subagent_ttl(f, envs) == ("5m", str(user_settings))
    write(folder / ".claude" / "settings.json", {KEY: "1h"})
    assert cli.effective_subagent_ttl(f, envs) == ("1h", project)
    write(local_settings(folder), {KEY: "5m"})
    assert cli.effective_subagent_ttl(f, envs) == ("5m", local)
    envs["CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL"] = "1h"
    assert cli.effective_subagent_ttl(f, envs) == (
        "1h", "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL")
    envs["FORCE_PROMPT_CACHING_5M"] = "1"
    assert cli.effective_subagent_ttl(f, envs) == ("5m",
                                                   "FORCE_PROMPT_CACHING_5M")


def test_the_repo_roots_local_file_is_read_first(env, unix, user_settings):
    """Inside a repository the root's settings.local.json wins over the
    starting directory's, so it has to be consulted before it."""
    repo = make_repo(env / "repo")
    sub = repo / "pkg"
    sub.mkdir()
    s = str(sub)
    root_local = cli.folder_settings_paths(cli.repo_local_settings_root(s))[0]
    write(user_settings, {KEY: "5m"})
    write(local_settings(sub), {KEY: "5m"})
    write(sub / ".claude" / "settings.json", {KEY: "5m"})
    assert cli.effective_subagent_ttl(s, {})[1] == local_settings_str(sub)
    write(repo / ".claude" / "settings.local.json", {KEY: "1h"})
    assert cli.effective_subagent_ttl(s, {}) == ("1h", root_local)
    # ...but the environment still beats every file.
    assert cli.effective_subagent_ttl(
        s, {"CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": "5m"})[0] == "5m"


def test_the_repo_root_is_not_consulted_on_windows(env, windows):
    repo = make_repo(env / "repo")
    sub = repo / "pkg"
    sub.mkdir()
    write(repo / ".claude" / "settings.local.json", {KEY: "1h"})
    assert cli.effective_subagent_ttl(str(sub), {}) == ("5m", "default")


def test_a_repo_root_folder_reads_its_own_file_once(env, unix):
    repo = make_repo(env / "repo")
    write(local_settings(repo), {KEY: "1h"})
    assert cli.effective_subagent_ttl(str(repo), {}) == (
        "1h", local_settings_str(repo))


def local_settings_str(folder):
    return cli.folder_settings_paths(str(folder))[0]


def test_invalid_values_and_unreadable_files_are_skipped(env, folder,
                                                         user_settings):
    f = str(folder)
    write(local_settings(folder), "{ broken")
    write(folder / ".claude" / "settings.json", {KEY: "10m"})
    write(user_settings, {KEY: "1h"})
    envs = {"CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": "2h"}
    assert cli.effective_subagent_ttl(f, envs) == ("1h", str(user_settings))


# --- the rule field is harmless to everything else ---------------------------

def test_the_record_does_not_disturb_status_list_or_the_hook(env, folder,
                                                             capsys):
    from niceclaude import hook
    assert on(folder, ttl="1h") == 0
    # A fresh, empty snapshot, so status runs through to its verdict rather
    # than stopping at "no snapshot yet".
    write(env / "data" / "state.json", {"ts_epoch": int(time.time()),
                                        "buckets": {}})
    capsys.readouterr()
    assert cli.cmd_list() == 0
    assert cli.SUBAGENT_TTL_RECORD in capsys.readouterr().out
    assert cli.cmd_status(str(folder)) == 0
    out = capsys.readouterr().out
    assert "paced         True" in out
    assert "snapshot age:" in out and "no snapshot yet" not in out
    pol = cli.load_policy()
    d = hook.decide(pol, {"ts_epoch": 0, "buckets": {}},
                    norm_path(str(folder)), now=10)
    assert d["paced"] is True
