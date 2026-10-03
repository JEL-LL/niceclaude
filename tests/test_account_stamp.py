"""The account stamp: a snapshot is only trusted by the account that wrote it.

Separate data dirs (test_account_paths) keep accounts apart only while their
paths differ. Under one NICECLAUDE_DIR they share a state.json, and a hook in
one account would pace on another account's usage whenever that snapshot was
fresh enough. So every snapshot carries the `config_key` of the process that
sampled, and hook.load_state ignores any other.

Ignoring is the whole point, not merely distrusting: a foreign snapshot that is
over the line, handed to decide(degraded=True), would brake "with full
confidence" on numbers that say nothing about this account. Loaded as {} it
forces a refresh instead, and a failed refresh leaves the hook honestly blind.

Every test here sets the key itself rather than inheriting one from conftest
or the shell.
"""

import json
import time

import pytest

from niceclaude import cli, hook
from niceclaude._shared import norm_path

pytestmark = pytest.mark.real_account_key

KEY_A = "/accounts/a/.claude-work"
KEY_B = "/accounts/b/.claude-home"
WORK = norm_path("/work")


def write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def snapshot(pct, stamp=..., age=0):
    """A fresh snapshot with one session bucket at `pct`. `stamp` omitted
    means no config_key at all, as old code wrote."""
    now = int(time.time()) - age
    st = {"ts_epoch": now, "ok": True,
          "buckets": {"session": {"pct": pct, "resets_epoch": now + 14400,
                                  "window_seconds": 18000, "label": None}}}
    if stamp is not ...:
        st["config_key"] = stamp
    return st


UNDER, OVER = 1, 90


@pytest.fixture
def hk(tmp_path, monkeypatch):
    """The hook's paths under tmp_path, a paced /work, and key A."""
    monkeypatch.setattr(hook, "POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setattr(hook, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(hook, "HOOK_LOG_PATH", str(tmp_path / "hook.log"))
    monkeypatch.setattr(hook, "ACCOUNT_KEY", KEY_A)
    write(tmp_path / "policy.json",
          {"paths": {WORK: {"paced": True, "enforce": "session"}},
           "defaults": {"chunk": 1}})
    return tmp_path


def hook_log(tmp_path):
    try:
        return (tmp_path / "hook.log").read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def refresher(monkeypatch, tmp_path, writes=None):
    """Replace refresh_snapshot with a zero-argument fake. With `writes` it
    publishes that snapshot and succeeds; without, it fails."""
    calls = []

    def fake():
        calls.append(1)
        if writes is None:
            return False
        write(tmp_path / "state.json", writes)
        return True

    monkeypatch.setattr(hook, "refresh_snapshot", fake)
    return calls


def stop_after(monkeypatch, naps=1):
    """Let `run` sleep `naps` times, without sleeping, then interrupt it."""
    taken = []

    def sleep(_s):
        taken.append(_s)
        if len(taken) >= naps:
            raise KeyboardInterrupt
    monkeypatch.setattr(hook.time, "sleep", sleep)
    return taken


# --- load_state ---------------------------------------------------------------

def test_load_state_accepts_its_own_stamp(tmp_path):
    st = snapshot(UNDER, KEY_A)
    write(tmp_path / "s.json", st)
    assert hook.load_state(str(tmp_path / "s.json"), KEY_A) == (st, None)


def test_load_state_rejects_another_accounts_stamp(tmp_path):
    write(tmp_path / "s.json", snapshot(UNDER, KEY_B))
    assert hook.load_state(str(tmp_path / "s.json"), KEY_A) == ({}, KEY_B)


def test_the_default_accounts_stamp_is_foreign_to_any_other(tmp_path):
    """"" is a real stamp, not a missing one, so it is reported as found."""
    write(tmp_path / "s.json", snapshot(UNDER, ""))
    assert hook.load_state(str(tmp_path / "s.json"), KEY_A) == ({}, "")


def test_an_unstamped_snapshot_is_foreign_to_a_non_default_key(tmp_path):
    write(tmp_path / "s.json", snapshot(UNDER))
    assert hook.load_state(str(tmp_path / "s.json"), KEY_A) == (
        {}, "<unstamped>")


def test_an_unstamped_snapshot_is_the_default_accounts(tmp_path):
    """The legacy rule: what old code wrote stays valid for a single-account
    user, who is the default account."""
    st = snapshot(UNDER)
    write(tmp_path / "s.json", st)
    assert hook.load_state(str(tmp_path / "s.json"), "") == (st, None)


@pytest.mark.parametrize("content", [None, "{not json", "[]", "42", "null"])
def test_absent_or_unreadable_is_not_foreign(tmp_path, content):
    path = tmp_path / "s.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert hook.load_state(str(path), KEY_A) == ({}, None)


def test_the_stamp_survives_publish_and_sample(tmp_path, monkeypatch):
    """What publish_state writes, load_state accepts under the same key only;
    and the usage.jsonl record carries the same stamp."""
    monkeypatch.setattr(cli, "ACCOUNT_KEY", KEY_A)
    monkeypatch.setattr(cli, "STATE_PATH", str(tmp_path / "state.json"))

    class P:
        stdout, stderr, returncode = "Current session: 3% used\n", "", 0
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: P())
    rec = cli.sample_once()
    assert rec["config_key"] == KEY_A
    cli.publish_state(rec)
    st, foreign = hook.load_state(cli.STATE_PATH, KEY_A)
    assert foreign is None and st["config_key"] == KEY_A
    assert hook.load_state(cli.STATE_PATH, KEY_B) == ({}, KEY_A)


# --- run ----------------------------------------------------------------------

def test_a_fresh_foreign_snapshot_forces_a_refresh(hk, monkeypatch):
    """Fresh and under the line, so trusted it would need no refresh at all
    (test_hook_ordering's fresh-snapshot case). Foreign, it is not trusted."""
    write(hk / "state.json", snapshot(UNDER, KEY_B))
    calls = refresher(monkeypatch, hk, writes=snapshot(UNDER, KEY_A))
    assert hook.run(WORK) == (None, "line-caught-up")
    assert calls == [1]


def test_a_foreign_snapshot_over_the_line_brakes_blind_not_confident(
        hk, monkeypatch):
    """The case the {} exists for. Its buckets would brake "with full
    confidence"; the hook must instead admit it cannot see."""
    write(hk / "state.json", snapshot(OVER, KEY_B))
    refresher(monkeypatch, hk)
    seen = []
    real = hook.decide
    monkeypatch.setattr(hook, "decide",
                        lambda *a, **k: seen.append(real(*a, **k)) or seen[-1])
    stop_after(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        hook.run(WORK)
    d = seen[-1]
    assert d["blind"] is True and d["hold"] == "hard"
    assert d["reason"] == "no usable buckets in snapshot"
    brake = [ln for ln in hook_log(hk).splitlines() if " brake " in ln]
    assert len(brake) == 1 and "session" not in brake[0]


def test_a_successful_refresh_is_read_through_the_second_load_site(
        hk, monkeypatch):
    """The foreign snapshot says over; our own fresh one says under. Only a
    reload after the refresh can turn that into a release."""
    write(hk / "state.json", snapshot(OVER, KEY_B))
    calls = refresher(monkeypatch, hk, writes=snapshot(UNDER, KEY_A))
    stop_after(monkeypatch)
    assert hook.run(WORK) == (None, "line-caught-up")
    assert calls == [1]


def test_an_unstamped_snapshot_is_trusted_under_the_default_key(
        hk, monkeypatch):
    monkeypatch.setattr(hook, "ACCOUNT_KEY", "")
    write(hk / "state.json", snapshot(UNDER))
    calls = refresher(monkeypatch, hk)
    assert hook.run(WORK) == (None, "line-caught-up")
    assert calls == []
    assert "foreign" not in hook_log(hk)


def test_an_unstamped_snapshot_is_refreshed_under_a_non_default_key(
        hk, monkeypatch):
    write(hk / "state.json", snapshot(UNDER))
    calls = refresher(monkeypatch, hk, writes=snapshot(UNDER, KEY_A))
    assert hook.run(WORK) == (None, "line-caught-up")
    assert calls == [1]
    assert "foreign snapshot (<unstamped>)" in hook_log(hk)


def test_foreign_snapshot_is_logged_once_per_invocation(hk, monkeypatch):
    """A failed refresh backs off, so the hold re-reads the same foreign file
    every chunk. One line says it; one per chunk would bury hook.log."""
    write(hk / "state.json", snapshot(UNDER, KEY_B))
    refresher(monkeypatch, hk)
    naps = stop_after(monkeypatch, naps=4)
    with pytest.raises(KeyboardInterrupt):
        hook.run(WORK)
    assert len(naps) == 4
    lines = [ln for ln in hook_log(hk).splitlines() if "foreign snapshot" in ln]
    assert len(lines) == 1
    assert f"foreign snapshot ({KEY_B})" in lines[0]


def test_an_unpaced_folder_never_refreshes_over_a_foreign_snapshot(
        hk, monkeypatch):
    """The cheap gate still comes first: test_hook_ordering's rule, with a
    snapshot that would otherwise demand a refresh."""
    write(hk / "policy.json", {"paths": {}})
    write(hk / "state.json", snapshot(OVER, KEY_B))
    calls = refresher(monkeypatch, hk)
    assert hook.run(WORK) == (None, "unpaced")
    assert calls == []
    assert hook_log(hk) == ""


def test_a_fresh_account_with_no_snapshot_logs_nothing_foreign(
        hk, monkeypatch):
    refresher(monkeypatch, hk)
    stop_after(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        hook.run(WORK)
    assert "foreign" not in hook_log(hk)


# --- status -------------------------------------------------------------------

@pytest.fixture
def st(tmp_path, monkeypatch):
    """A paced folder for `status`, key A, and every log path under tmp_path,
    so a write to hook.log would show."""
    monkeypatch.setattr(cli, "POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setattr(cli, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(cli, "ACCOUNT_KEY", KEY_A)
    monkeypatch.setattr(hook, "ACCOUNT_KEY", KEY_A)
    monkeypatch.setattr(cli, "HOOK_LOG_PATH", str(tmp_path / "hook.log"))
    monkeypatch.setattr(hook, "HOOK_LOG_PATH", str(tmp_path / "hook.log"))
    monkeypatch.setattr(cli, "ROOT_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "DATA_DIR", str(tmp_path))
    work = tmp_path / "work"
    work.mkdir()
    cli.save_policy({"paths": {norm_path(str(work)): {"paced": True}}})
    return work


def test_status_ignores_a_foreign_snapshot(st, tmp_path, capsys):
    write(tmp_path / "state.json", snapshot(OVER, KEY_B))
    assert cli.cmd_status(str(st)) == 0
    out = capsys.readouterr().out
    assert f"snapshot: written by another account ({KEY_B}) -- ignored" in out
    assert "OVER" not in out and "HOLDS" not in out
    assert not (tmp_path / "hook.log").exists()


def test_status_names_the_default_accounts_snapshot_readably(st, tmp_path,
                                                             capsys):
    write(tmp_path / "state.json", snapshot(UNDER, ""))
    assert cli.cmd_status(str(st)) == 0
    assert ("snapshot: written by another account (<default>) -- ignored"
            in capsys.readouterr().out)


def test_status_on_an_unreadable_snapshot(st, tmp_path, capsys):
    (tmp_path / "state.json").write_text("{trunc", encoding="utf-8")
    assert cli.cmd_status(str(st)) == 0
    assert ("snapshot: unreadable -- delete it or restart niceclaude watch"
            in capsys.readouterr().out)


def test_status_with_no_snapshot_is_unchanged(st, capsys):
    assert cli.cmd_status(str(st)) == 0
    out = capsys.readouterr().out
    assert "no snapshot yet -- is `niceclaude watch` running?" in out
    assert "another account" not in out and "unreadable" not in out


def test_status_judges_its_own_snapshot_as_before(st, tmp_path, capsys):
    write(tmp_path / "state.json", snapshot(UNDER, KEY_A))
    assert cli.cmd_status(str(st)) == 0
    assert "snapshot age:" in capsys.readouterr().out


# --- the upgrade warnings -----------------------------------------------------

WARNING = ("a daemon holding the legacy pidfile has not published a stamped "
           "snapshot; if it predates this version, stop it from a shell with "
           "CLAUDE_CONFIG_DIR unset and restart `watch`")


@pytest.fixture
def split(st, tmp_path, monkeypatch):
    """ROOT_DIR and DATA_DIR apart, as for a non-default account, and no live
    pid unless a test says so."""
    root, data = tmp_path / "root", tmp_path / "data"
    root.mkdir()
    data.mkdir()
    monkeypatch.setattr(cli, "ROOT_DIR", str(root))
    monkeypatch.setattr(cli, "DATA_DIR", str(data))
    monkeypatch.setattr(cli, "pid_alive", lambda pid: False)
    return root


def warned(capsys):
    # Wrapped for the terminal, so compared word for word, not byte for byte.
    return WARNING in " ".join(capsys.readouterr().out.split())


def test_warning_a_a_fresh_unstamped_root_snapshot(st, split, capsys):
    write(split / "state.json", snapshot(UNDER))
    cli.cmd_status(str(st))
    assert warned(capsys)


def test_no_warning_a_for_a_stale_unstamped_root_snapshot(st, split, capsys):
    write(split / "state.json", snapshot(UNDER, age=cli.MAX_STALE + 60))
    cli.cmd_status(str(st))
    assert not warned(capsys)


def test_warning_b_a_live_root_pid_and_no_stamp(st, split, monkeypatch,
                                               capsys):
    write(split / "state.json", snapshot(UNDER, age=cli.MAX_STALE + 60))
    (split / "daemon.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cli, "pid_alive", lambda pid: pid == 4242)
    cli.cmd_status(str(st))
    assert warned(capsys)


def test_no_warning_b_when_the_root_snapshot_is_stamped_default(
        st, split, monkeypatch, capsys):
    """The default account's own new daemon owns the root pidfile in ordinary
    two-account use. A work account must not be told to kill it."""
    write(split / "state.json", snapshot(UNDER, "", age=cli.MAX_STALE + 60))
    (split / "daemon.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)
    cli.cmd_status(str(st))
    assert not warned(capsys)


def test_no_warning_when_the_data_dir_is_the_root(st, tmp_path, monkeypatch,
                                                  capsys):
    write(tmp_path / "state.json", snapshot(UNDER))
    (tmp_path / "daemon.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)
    cli.cmd_status(str(st))
    assert not warned(capsys)


@pytest.mark.parametrize("sign", ["a", "b"])
def test_both_warnings_reach_an_unpaced_folder(st, split, tmp_path,
                                               monkeypatch, capsys, sign):
    """Placed before the `matched is None` return: a pre-upgrade daemon
    misleads the hooks whatever this folder's policy is."""
    age = 0 if sign == "a" else cli.MAX_STALE + 60
    write(split / "state.json", snapshot(UNDER, age=age))
    if sign == "b":
        (split / "daemon.pid").write_text("4242", encoding="utf-8")
        monkeypatch.setattr(cli, "pid_alive", lambda pid: True)
    unpaced = tmp_path / "elsewhere"
    unpaced.mkdir()
    assert cli.cmd_status(str(unpaced)) == 0
    out = capsys.readouterr().out
    assert "NOT paced" in out
    assert WARNING in " ".join(out.split())
