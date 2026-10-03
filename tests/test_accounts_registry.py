"""The account registry, and what `status` says about accounts (plan Phase 4).

`install` is per account: it writes into the settings.json of the config dir
CLAUDE_CONFIG_DIR names. So an account nobody ran `install` from has no hook
at all, and nothing in that account can say so. The registry is how one
account's `status` can see the others: `install` records each config dir in
REGISTRY_PATH, `uninstall` marks it off without forgetting it, and `status`
lists the union of the registry and the `accounts/*` directories.

Every test here runs under tmp_path. The registry is pinned by conftest's
autouse fixture; the root, the data dir, the policy and the fragment are
pinned here, and the home is redirected, so the default-account comparison is
against tmp_path's `.claude`, never the developer's.
"""

import json
import os
import time
import types

import pytest

from niceclaude import cli, hook
from niceclaude._shared import account_slug, norm_config_dir

HOOK = "/home/me/.local/bin/niceclaude-hook"


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def our_hook(config_dir):
    """A settings.json in `config_dir` with our hook, as install leaves it."""
    os.makedirs(config_dir, exist_ok=True)
    cfg = {"hooks": {ev: [{"matcher": "*", "hooks": [
        {"type": "command", "command": HOOK}]}] for ev in cli.HOOK_EVENTS}}
    with open(os.path.join(config_dir, "settings.json"), "w",
              encoding="utf-8") as fh:
        json.dump(cfg, fh)


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A machine under tmp_path: root, home, and two account config dirs."""
    root = tmp_path / "root"
    home = tmp_path / "home"
    root.mkdir()
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli, "ROOT_DIR", str(root))
    monkeypatch.setattr(cli, "DATA_DIR", str(root))
    monkeypatch.setattr(cli, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(cli, "SETTINGS_PATH",
                        str(tmp_path / "config" / "settings.json"))
    monkeypatch.setattr(cli, "POLICY_PATH", str(root / "policy.json"))
    monkeypatch.setattr(cli, "STATE_PATH", str(root / "state.json"))
    monkeypatch.setattr(cli, "CLAUDE_SETTINGS_MARKER_PATH",
                        str(root / "claude_settings_marker.json"))
    monkeypatch.setattr(cli, "find_hook_exe", lambda: HOOK)
    monkeypatch.setattr(cli, "pid_alive", lambda pid: False)
    return {"root": root, "home": home, "a": tmp_path / "claude-a",
            "b": tmp_path / "claude-b"}


def install_in(monkeypatch, config_dir, force=False):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    return cli.cmd_install(force=force)


def without_ts(reg):
    return {k: {f: v for f, v in e.items() if f != "ts"}
            for k, e in reg.items()}


# --- install and uninstall ---------------------------------------------------

def test_two_installs_in_one_process_record_two_accounts(box, monkeypatch):
    """Keyed from os.environ at call time, so the autouse fixture's pinned
    ACCOUNT_KEY of "" does not fold both onto the default entry."""
    assert cli.ACCOUNT_KEY == ""
    assert install_in(monkeypatch, box["a"]) == 0
    assert install_in(monkeypatch, box["b"]) == 0
    reg = read(cli.REGISTRY_PATH)
    ka, kb = norm_config_dir(str(box["a"])), norm_config_dir(str(box["b"]))
    assert set(reg) == {ka, kb}
    for key, cdir in ((ka, box["a"]), (kb, box["b"])):
        assert reg[key]["config_dir"] == os.path.abspath(str(cdir))
        assert reg[key]["slug"] == account_slug(key)
        assert reg[key]["hook"] is True
        assert reg[key]["ts"]


def test_install_is_idempotent_in_the_registry(box, monkeypatch):
    install_in(monkeypatch, box["a"])
    install_in(monkeypatch, box["b"])
    first = without_ts(read(cli.REGISTRY_PATH))
    install_in(monkeypatch, box["b"])
    install_in(monkeypatch, box["a"])
    assert without_ts(read(cli.REGISTRY_PATH)) == first


def test_a_set_to_default_config_dir_is_the_default_entry(box, monkeypatch):
    install_in(monkeypatch, box["home"] / ".claude")
    reg = read(cli.REGISTRY_PATH)
    assert list(reg) == [""]
    assert reg[""]["slug"] == ""


def test_uninstall_marks_the_entry_off_without_dropping_it(box, monkeypatch):
    install_in(monkeypatch, box["a"])
    install_in(monkeypatch, box["b"])
    before = read(cli.REGISTRY_PATH)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(box["a"]))
    assert cli.cmd_uninstall() == 0
    after = read(cli.REGISTRY_PATH)
    ka = norm_config_dir(str(box["a"]))
    assert set(after) == set(before)
    assert after[ka]["hook"] is False
    assert after[ka]["config_dir"] == before[ka]["config_dir"]
    assert after[ka]["slug"] == before[ka]["slug"]
    others = [k for k in after if k != ka]
    assert [after[k] for k in others] == [before[k] for k in others]


def test_an_unreadable_registry_is_replaced_not_fatal(box, monkeypatch):
    with open(cli.REGISTRY_PATH, "w", encoding="utf-8") as fh:
        fh.write("[1, 2")
    assert install_in(monkeypatch, box["a"]) == 0
    assert list(read(cli.REGISTRY_PATH)) == [norm_config_dir(str(box["a"]))]


def test_install_force_names_the_other_accounts(box, monkeypatch, capsys):
    install_in(monkeypatch, box["b"])
    capsys.readouterr()
    assert install_in(monkeypatch, box["a"], force=True) == 0
    out = capsys.readouterr().out
    assert "every account" in out
    assert f"also used by: {os.path.abspath(str(box['b']))}" in out
    assert f"also used by: {os.path.abspath(str(box['a']))}" not in out


def test_install_without_force_says_nothing_of_other_accounts(box, monkeypatch,
                                                              capsys):
    install_in(monkeypatch, box["b"])
    install_in(monkeypatch, box["a"])
    assert "also used by" not in capsys.readouterr().out


# --- status: the account list ------------------------------------------------

@pytest.fixture
def unpaced(box, tmp_path, monkeypatch):
    """An unpaced folder, so every line checked is one printed before the
    `matched is None` return. NICECLAUDE_DIR is dropped: under it every
    account shares the root (D3), and the history hint never applies."""
    monkeypatch.delenv("NICECLAUDE_DIR", raising=False)
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    cli.save_policy({"paths": {}})
    return str(folder)


def status(folder, capsys):
    assert cli.cmd_status(folder) == 0
    out = capsys.readouterr().out
    assert "NOT paced" in out
    return out


def accounts_block(out):
    lines = out.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("accounts:"))
    block = [lines[start][16:]]
    for line in lines[start + 1:]:
        if not line.startswith(" " * 16):
            break
        block.append(line[16:])
    return block


def register(key, config_dir, hooked=True):
    reg = cli.load_registry()
    reg[key] = {"config_dir": str(config_dir), "slug": account_slug(key),
                "hook": hooked, "ts": "2026-01-01T00:00:00Z"}
    cli.write_atomic(cli.REGISTRY_PATH, json.dumps(reg))


def test_status_names_this_account(unpaced, monkeypatch, capsys):
    out = status(unpaced, capsys)
    assert "account:        <default>" in out
    monkeypatch.setattr(cli, "ACCOUNT_KEY", "/some/work")
    assert "account:        /some/work" in status(unpaced, capsys)


def test_status_reads_the_hook_live_from_each_config_dir(box, unpaced,
                                                         capsys):
    ka = norm_config_dir(str(box["a"]))
    kb = norm_config_dir(str(box["b"]))
    our_hook(str(box["a"]))
    register(ka, box["a"], hooked=False)    # live beats the stored flag
    register(kb, box["b"], hooked=True)     # stored true, but no hook there
    block = accounts_block(status(unpaced, capsys))
    assert f"  {box['a']}: hook registered; no daemon" in block
    assert f"  {box['b']}: hook NOT registered; no daemon" in block


def test_a_stored_hook_false_shows_as_uninstalled_on_purpose(box, unpaced,
                                                             capsys):
    ka = norm_config_dir(str(box["a"]))
    register(ka, box["a"], hooked=False)
    block = accounts_block(status(unpaced, capsys))
    assert f"  {box['a']}: (uninstalled on purpose); no daemon" in block


def test_an_account_dir_with_no_entry_shows_its_slug(box, unpaced, capsys):
    (box["root"] / "accounts" / "work-1a2b3c4d").mkdir(parents=True)
    (box["root"] / "accounts" / "stray.txt").write_text("", encoding="utf-8")
    block = accounts_block(status(unpaced, capsys))
    assert "  no install recorded for work-1a2b3c4d; no daemon" in block
    assert not any("stray.txt" in line for line in block)


def test_a_registered_accounts_dir_is_not_listed_twice(box, unpaced, capsys):
    ka = norm_config_dir(str(box["a"]))
    (box["root"] / "accounts" / account_slug(ka)).mkdir(parents=True)
    register(ka, box["a"])
    block = accounts_block(status(unpaced, capsys))
    assert len(block) == 1
    assert "no install recorded" not in block[0]


def test_status_shows_each_accounts_daemon(box, unpaced, monkeypatch, capsys):
    ka = norm_config_dir(str(box["a"]))
    adir = box["root"] / "accounts" / account_slug(ka)
    adir.mkdir(parents=True)
    (adir / "daemon.pid").write_text("4242", encoding="utf-8")
    (box["root"] / "daemon.pid").write_text("99", encoding="utf-8")
    register("", box["home"] / ".claude")
    register(ka, box["a"])
    monkeypatch.setattr(cli, "pid_alive", lambda pid: pid == 4242)
    block = accounts_block(status(unpaced, capsys))
    assert block[0].startswith("* <default>:")         # this shell, first
    assert block[0].endswith("; no daemon")              # pid 99 is dead
    assert block[1] == (f"  {box['a']}: hook NOT registered; "
                        f"daemon running (pid 4242)")


def test_status_marks_this_account_when_it_is_not_the_default(
        box, unpaced, monkeypatch, capsys):
    ka = norm_config_dir(str(box["a"]))
    register(ka, box["a"])
    monkeypatch.setattr(cli, "ACCOUNT_KEY", ka)
    assert accounts_block(status(unpaced, capsys))[0].startswith(
        f"* {box['a']}:")


def test_no_accounts_line_when_none_is_known(unpaced, capsys):
    assert "accounts:" not in status(unpaced, capsys)


def test_a_pathologically_nested_registry_crashes_nothing(
        unpaced, box, monkeypatch, capsys):
    with open(cli.REGISTRY_PATH, "w", encoding="utf-8") as fh:
        fh.write("[" * 200000)
    assert "accounts:" not in status(unpaced, capsys)
    assert install_in(monkeypatch, box["a"]) == 0      # and replaces it
    assert isinstance(cli.load_registry(), dict) and cli.load_registry()


# --- status: the relative config dir (D4) ------------------------------------

@pytest.mark.parametrize("value", ["~/.claude-work", "claude-work"])
def test_a_relative_config_dir_is_warned_about(unpaced, monkeypatch, capsys,
                                               value):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)
    out = " ".join(status(unpaced, capsys).split())
    assert f"CLAUDE_CONFIG_DIR is relative ({value})" in out
    assert "does not expand ~" in out


def test_an_absolute_config_dir_is_not_warned_about(box, unpaced, monkeypatch,
                                                    capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(box["a"]))
    assert "is relative" not in status(unpaced, capsys)


# --- status: the legacy-history hint (D9) ------------------------------------

KEY = "/some/work"
CAVEAT = ("only if that history was recorded by this account alone; a mixed "
          "log cannot be split")


@pytest.fixture
def stranded(box, unpaced, tmp_path, monkeypatch):
    """A non-default account with its own empty data dir, and history in the
    root beside every shared file the hint must never name."""
    data = box["root"] / "accounts" / "work-1a2b3c4d"
    data.mkdir(parents=True)
    monkeypatch.setattr(cli, "ACCOUNT_KEY", KEY)
    monkeypatch.setattr(cli, "DATA_DIR", str(data))
    monkeypatch.setattr(cli, "STATE_PATH", str(data / "state.json"))
    for name in ("usage.jsonl", "hook.log", "state.json",
                 "claude_settings_marker.json", "accounts.json"):
        (box["root"] / name).write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(cli, "REGISTRY_PATH",
                        str(box["root"] / "accounts.json"))
    return data


def mv_lines(out):
    return [line.strip() for line in out.splitlines()
            if line.strip().startswith("mv ")]


def test_the_hint_prints_one_resolved_mv_per_history_file(box, unpaced,
                                                          stranded, capsys):
    out = status(unpaced, capsys)
    assert CAVEAT in " ".join(out.split())
    root, data = box["root"], stranded
    assert mv_lines(out) == [
        f'mv "{os.path.abspath(root / name)}" "{os.path.abspath(data / name)}"'
        for name in ("usage.jsonl", "hook.log")]
    for line in mv_lines(out):
        for never in ("state.json", "policy.json", "claude_settings_marker",
                      "accounts.json"):
            assert never not in line


def test_the_hint_names_only_the_files_that_exist(box, unpaced, stranded,
                                                  capsys):
    os.remove(box["root"] / "usage.jsonl")
    lines = mv_lines(status(unpaced, capsys))
    assert len(lines) == 1 and "hook.log" in lines[0]


def test_the_hint_warns_before_replacing_an_existing_hook_log(
        unpaced, stranded, capsys):
    (stranded / "hook.log").write_text("mine\n", encoding="utf-8")
    assert "mv would replace it" in status(unpaced, capsys)


def test_the_hint_makes_a_missing_data_dir_first(unpaced, stranded, capsys):
    os.rmdir(stranded)
    out = status(unpaced, capsys)
    assert f'mkdir -p "{stranded}"' in out
    assert len(mv_lines(out)) == 2


@pytest.mark.parametrize("unmet", [
    "default key", "NICECLAUDE_DIR set", "no root history",
    "account has a log"])
def test_the_hint_needs_all_four_conditions(box, unpaced, stranded,
                                            monkeypatch, capsys, unmet):
    if unmet == "default key":
        monkeypatch.setattr(cli, "ACCOUNT_KEY", "")
    elif unmet == "NICECLAUDE_DIR set":
        monkeypatch.setenv("NICECLAUDE_DIR", str(box["root"]))
    elif unmet == "no root history":
        os.remove(box["root"] / "usage.jsonl")
        os.remove(box["root"] / "hook.log")
    else:
        (stranded / "usage.jsonl").write_text("", encoding="utf-8")
    out = status(unpaced, capsys)
    assert mv_lines(out) == []
    assert CAVEAT not in " ".join(out.split())


def test_the_hint_and_the_legacy_daemon_warning_are_separate(
        box, unpaced, stranded, capsys):
    """The Phase 2 warning still prints once, alongside the hint, not merged
    into it: the root snapshot here is unstamped and fresh."""
    snap = {"ts_epoch": int(time.time()), "buckets": {}}
    (box["root"] / "state.json").write_text(json.dumps(snap), encoding="utf-8")
    out = " ".join(status(unpaced, capsys).split())
    assert out.count(cli.LEGACY_DAEMON_WARNING) == 1
    assert CAVEAT in out


# --- help --------------------------------------------------------------------

def page(capsys, name):
    with pytest.raises(SystemExit):
        cli.main([name, "--help"])
    return " ".join(capsys.readouterr().out.split())


def test_install_help_says_force_reaches_every_account(capsys):
    text = page(capsys, "install")
    assert "resets the rules of EVERY account" in text
    assert "names the other accounts it has recorded" in text
    assert "accounts.json" in text


def test_uninstall_help_says_the_account_is_kept(capsys):
    assert "uninstalled on purpose" in page(capsys, "uninstall")


def test_status_help_describes_the_account_lines(capsys):
    text = page(capsys, "status")
    assert "names this shell's account" in text
    assert "whether its daemon is running" in text
    assert "relative CLAUDE_CONFIG_DIR" in text


def test_the_hook_reads_no_registry(box, monkeypatch):
    """The hot path stays as it was: nothing in the hook module knows the
    registry exists."""
    assert "REGISTRY_PATH" not in vars(hook)


# --- review fixes -------------------------------------------------------------

def test_force_never_lists_this_account_when_the_registry_write_fails(
        box, monkeypatch, capsys):
    install_in(monkeypatch, box["a"])          # this account, recorded
    capsys.readouterr()
    real = cli.write_atomic

    def failing(path, text):
        if path == cli.REGISTRY_PATH:
            raise OSError("read-only")
        return real(path, text)

    monkeypatch.setattr(cli, "write_atomic", failing)
    assert install_in(monkeypatch, box["a"], force=True) == 0
    out = capsys.readouterr().out
    assert "also used by" not in out


@pytest.mark.parametrize("returncode, removed", [(0, True), (1, False)])
def test_stop_on_windows_removes_the_pidfile_taskkill_leaves(
        tmp_path, monkeypatch, capsys, returncode, removed):
    pidfile = tmp_path / "daemon.pid"
    pidfile.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cli, "PID_PATH", str(pidfile))
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)
    monkeypatch.setattr(cli.os, "name", "nt")
    calls = []
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda argv, **kw: calls.append(argv) or
        types.SimpleNamespace(returncode=returncode))
    assert cli.cmd_stop() == 0
    assert calls and calls[0][:3] == ["taskkill", "/PID", "4242"]
    assert pidfile.exists() is not removed


def test_a_shared_niceclaude_dir_daemon_is_not_claimed_for_every_account(
        box, monkeypatch, capsys):
    """D3: one pidfile at the root for every account, so its daemon is not
    attributed to any one of them."""
    monkeypatch.setenv("NICECLAUDE_DIR", str(box["root"]))
    cli.save_policy({"paths": {}})
    folder = box["root"].parent / "elsewhere"
    folder.mkdir()
    register(norm_config_dir(str(box["a"])), box["a"])
    register(norm_config_dir(str(box["b"])), box["b"])
    (box["root"] / "daemon.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)
    lines = [ln for ln in status(str(folder), capsys).splitlines()
             if "daemon running" in ln]
    assert len(lines) == 2
    assert all("may be another's" in ln for ln in lines)
