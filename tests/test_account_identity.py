"""The account identity: which login a config dir held when it sampled.

The config key (test_account_stamp) tells config dirs apart, and that is what
pacing needs. It cannot see a `/login` to a different account inside one dir,
or two dirs logged in to the same account. So the CLI reads
`oauthAccount.{accountUuid, organizationUuid}` from Claude's global config,
stamps the pair into every record and snapshot as `account`, and `check`
reports what it shows. Diagnostic only: nothing paces on it, and the hook never
opens the file.

The same object in that file holds an email address and a name. Only the two
UUIDs may ever leave the reader, so the first test plants both and searches
everything niceclaude writes or prints for them.

Every config file here lives under tmp_path; the developer's real
`.claude.json` is never opened.
"""

import builtins
import json
import os
import time

import pytest

from niceclaude import _shared, cli, hook
from niceclaude._shared import norm_path

ACCT = "11111111-2222-3333-4444-555555555555"
ORG = "66666666-7777-8888-9999-aaaaaaaaaaaa"
OTHER_ACCT = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
OTHER_ORG = "00000000-1111-2222-3333-444444444444"
PAIR = {"accountUuid": ACCT, "organizationUuid": ORG}
OTHER = {"accountUuid": OTHER_ACCT, "organizationUuid": OTHER_ORG}

EMAIL = "fixture.person@example.invalid"
FULL_NAME = "Fixture Quentin Person"
DISPLAY_NAME = "Quentin"


def oauth(acct=ACCT, org=ORG):
    """A fake `oauthAccount`, carrying the fields that must never leak."""
    return {"accountUuid": acct, "organizationUuid": org,
            "emailAddress": EMAIL, "fullName": FULL_NAME,
            "displayName": DISPLAY_NAME, "organizationRole": "admin"}


def write(path, obj):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """CLAUDE_CONFIG_DIR at tmp_path/cfg; returns where `.claude.json` goes."""
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    return d / ".claude.json"


class Usage:
    stdout, stderr, returncode = "Current session: 3% used\n", "", 0


@pytest.fixture
def cli_paths(tmp_path, monkeypatch):
    """cli's log, state and root under tmp_path, and a faked `/usage`."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(cli, "ROOT_DIR", str(root))
    monkeypatch.setattr(cli, "LOG_PATH", str(root / "usage.jsonl"))
    monkeypatch.setattr(cli, "STATE_PATH", str(root / "state.json"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: Usage())
    return root


@pytest.fixture
def opened(monkeypatch):
    """Every path handed to builtins.open from here on."""
    seen = []
    real = builtins.open

    def spy(file, *a, **k):
        seen.append(str(file))
        return real(file, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    return seen


# --- privacy ------------------------------------------------------------------

def test_only_the_two_uuids_ever_leave_the_config(cfg, cli_paths, capsys):
    """Sample, publish, print and check, with a config holding an email and a
    name beside the UUIDs. Neither may appear in anything written or shown."""
    write(cfg, {"oauthAccount": oauth(), "userID": "not-an-account-id"})
    assert cli.main(["sample"]) == 0
    assert cli.main(["refresh"]) == 0
    cli.cmd_check()
    out, err = capsys.readouterr()

    state_text = (cli_paths / "state.json").read_text(encoding="utf-8")
    log_text = (cli_paths / "usage.jsonl").read_text(encoding="utf-8")
    for secret in (EMAIL, FULL_NAME, DISPLAY_NAME, "admin",
                   "not-an-account-id"):
        for where, text in (("state.json", state_text),
                            ("usage.jsonl", log_text),
                            ("stdout", out), ("stderr", err)):
            assert secret not in text, f"{secret!r} leaked into {where}"

    # Kept: the pair, and exactly the pair.
    assert json.loads(state_text)["account"] == PAIR
    for line in log_text.splitlines():
        assert json.loads(line)["account"] == PAIR


# --- the reader ---------------------------------------------------------------

@pytest.mark.parametrize("content", [
    None,                                   # no file at all
    "{not json",                            # malformed
    "",                                     # empty
    json.dumps({"numStartups": 3}),          # API-key user: no oauthAccount
    json.dumps([oauth()]),                  # not an object at top level
    json.dumps({"oauthAccount": None}),
    json.dumps({"oauthAccount": [ACCT, ORG]}),
    json.dumps({"oauthAccount": {"accountUuid": ACCT}}),   # half a pair
    json.dumps({"oauthAccount": {"accountUuid": ACCT,
                                 "organizationUuid": 7}}),
    # Not UUID-shaped, so not trusted to be one: nothing else may ride along.
    json.dumps({"oauthAccount": {"accountUuid": EMAIL,
                                 "organizationUuid": ORG}}),
    # 36 characters of the right alphabet, but not 8-4-4-4-12.
    json.dumps({"oauthAccount": {"accountUuid": "-" * 36,
                                 "organizationUuid": ORG}}),
    json.dumps({"oauthAccount": {"accountUuid": "a" * 36,
                                 "organizationUuid": ORG}}),
])
def test_an_unreadable_or_unlogged_config_is_unknown(cfg, content):
    if content is not None:
        cfg.write_text(content, encoding="utf-8")
    assert cli.read_account() is None


def test_undecodable_bytes_are_unknown(cfg):
    cfg.write_bytes(b'{"oauthAccount": "\xff\xfe"}')
    assert cli.read_account() is None


def test_the_reader_keeps_the_pair_and_nothing_else(cfg):
    write(cfg, {"oauthAccount": oauth()})
    assert cli.read_account() == PAIR


def test_projects_keys_differing_only_by_case_still_yield_the_pair(cfg):
    """The real file has `projects` keys differing only in drive-letter case.
    Python keeps both; a case-folding parser would refuse the file."""
    cfg.write_text(
        '{"projects": {"C:/work": {"a": 1}, "c:/work": {"a": 2}},'
        ' "oauthAccount": ' + json.dumps(oauth()) + '}', encoding="utf-8")
    assert cli.read_account() == PAIR


def test_a_duplicated_oauth_account_yields_the_last(cfg):
    cfg.write_text(
        '{"oauthAccount": ' + json.dumps(oauth(OTHER_ACCT, OTHER_ORG)) + ','
        ' "oauthAccount": ' + json.dumps(oauth()) + '}', encoding="utf-8")
    assert cli.read_account() == PAIR


def test_the_file_is_resolved_at_call_time(tmp_path, monkeypatch):
    """Set: <CLAUDE_CONFIG_DIR>/.claude.json. Unset: ~/.claude.json, beside
    the default dir rather than in it. Re-read on every call."""
    home = tmp_path / "home"
    monkeypatch.setattr(cli, "HOME", str(home))
    write(home / ".claude.json", {"oauthAccount": oauth()})
    write(tmp_path / "w" / ".claude.json",
          {"oauthAccount": oauth(OTHER_ACCT, OTHER_ORG)})

    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert cli.claude_global_config_path() == str(home / ".claude.json")
    assert cli.read_account() == PAIR
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "w"))
    assert cli.read_account() == OTHER


@pytest.mark.real_account_key
def test_the_file_is_never_derived_from_the_account_key(cfg, monkeypatch):
    """The key is a normalized form for comparison, not a path Claude opens."""
    write(cfg, {"oauthAccount": oauth()})
    monkeypatch.setattr(cli, "ACCOUNT_KEY", "/elsewhere/.claude-other")
    assert cli.read_account() == PAIR


# --- the set-to-default case (Phase 0, X2) -------------------------------------

def test_a_set_to_default_config_dir_with_no_oauth_account_is_unknown(
        tmp_path, monkeypatch, cli_paths, capsys):
    """CLAUDE_CONFIG_DIR=~/.claude is the default account, but Claude then
    reads ~/.claude/.claude.json, which may be fresh and not logged into.
    The reader reports None rather than falling back to ~/.claude.json, and
    `check` treats the gap as unknown, not as a login change."""
    home = tmp_path / "home"
    monkeypatch.setattr(cli, "HOME", str(home))
    write(home / ".claude.json", {"oauthAccount": oauth()})
    write(home / ".claude" / ".claude.json", {"numStartups": 1})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    # Computed from the value actually set: conftest pins cli.ACCOUNT_KEY to ""
    # for every test, so asserting on that would prove nothing.
    assert _shared.config_key(str(home / ".claude"), str(home)) == ""
    assert cli.read_account() is None

    cli.append_log(dict(sample_record(1), account=PAIR))
    cli.append_log(cli.sample_once())   # sampled under the set-to-default dir
    cli.append_log(dict(sample_record(3), account=PAIR))
    records = cli.load_log()
    assert [r.get("account") for r in records] == [PAIR, None, PAIR]

    cli.cmd_check()
    out = capsys.readouterr().out
    assert "login changed" not in out


# --- check: a change within one directory --------------------------------------

def sample_record(i, account=...):
    """A parseable record, one minute apart; `account` omitted means none, as
    every record before Phase 3 was written."""
    now = int(time.time())
    rec = {"ts": f"t{i}", "ts_epoch": now - (10 - i) * 60, "exit_code": 0,
           "elapsed_ms": 1, "stderr": None,
           "raw": f"Current session: {i}% used\n",
           "buckets": {}, "unparsed_lines": []}
    if account is not ...:
        rec["account"] = account
    return rec


def write_log(path, records):
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def test_check_flags_a_mid_log_login_change(cli_paths, capsys):
    write_log(cli_paths / "usage.jsonl", [
        sample_record(1, PAIR), sample_record(2), sample_record(3, None),
        sample_record(4, PAIR), sample_record(5, OTHER),
        sample_record(6, OTHER)])
    cli.cmd_check()
    notes = [ln for ln in capsys.readouterr().out.splitlines()
             if "login changed" in ln]
    assert len(notes) == 1
    assert notes[0].startswith("note: t5 ") and "last seen t4" in notes[0]
    assert ACCT not in notes[0] and OTHER_ACCT not in notes[0]


def test_case_never_makes_a_different_account(cfg, cli_paths, capsys):
    upper = {k: v.upper() for k, v in PAIR.items()}
    write(cfg, {"oauthAccount": upper})
    assert cli.read_account() == PAIR
    write_log(cli_paths / "usage.jsonl",
              [sample_record(1, PAIR), sample_record(2, upper)])
    cli.cmd_check()
    assert "login changed" not in capsys.readouterr().out


def test_an_organization_switch_is_a_login_change(cli_paths, capsys):
    """Limits attach to the seat, so the same person in another org counts."""
    write_log(cli_paths / "usage.jsonl", [
        sample_record(1, PAIR), sample_record(2, dict(PAIR, organizationUuid=
                                                      OTHER_ORG))])
    cli.cmd_check()
    assert "login changed" in capsys.readouterr().out


def test_a_login_change_is_a_note_not_a_problem(cli_paths, capsys):
    write_log(cli_paths / "usage.jsonl",
              [sample_record(1, PAIR), sample_record(2, OTHER)])
    assert cli.cmd_check() == 0
    out = capsys.readouterr().out
    assert "login changed" in out and "no anomalies" in out


def test_records_without_an_account_are_never_a_change(cli_paths, capsys):
    """Old records carry no `account`; unknown is not different."""
    write_log(cli_paths / "usage.jsonl",
              [sample_record(1), sample_record(2, PAIR), sample_record(3),
               sample_record(4, None), sample_record(5, PAIR)])
    assert cli.cmd_check() == 0
    assert "login changed" not in capsys.readouterr().out


# --- check: one login in two directories ----------------------------------------

def test_check_flags_two_account_dirs_sharing_a_login(
        cli_paths, capsys, opened):
    """Reads only state.json files. Another account's usage.jsonl is never
    opened -- this account has no log at all here."""
    root = cli_paths
    acc = root / "accounts"
    write(root / "state.json", {"config_key": "", "account": PAIR})
    write(acc / "work-1" / "state.json", {"config_key": "/w", "account": PAIR,
                                          "ts": "2026-09-12T08:00:00Z"})
    write(acc / "home-2" / "state.json", {"config_key": "/h", "account": OTHER})
    write(acc / "orgs-3" / "state.json",       # same person, another seat
          {"account": dict(PAIR, organizationUuid=OTHER_ORG)})
    write(acc / "none-4" / "state.json", {"config_key": "/n", "account": None})
    write(acc / "old-5" / "state.json", {"ts_epoch": 1})
    (acc / "work-1" / "usage.jsonl").write_text("{garbage\n", encoding="utf-8")
    opened.clear()

    assert cli.cmd_check() == 1            # no records of our own
    out, err = capsys.readouterr()
    notes = [ln for ln in out.splitlines() if "one login in" in ln]
    assert notes == [f"note: one login in 2 account directories: "
                     f"{root}, {acc / 'work-1'} "
                     f"(last sampled 2026-09-12T08:00:00Z)"]
    assert not [p for p in opened if p.endswith("usage.jsonl")
                and os.path.dirname(p) != str(root)]
    assert "corrupt JSON" not in err


def test_a_pathologically_nested_sibling_snapshot_does_not_crash_check(
        cli_paths, capsys):
    write(cli_paths / "state.json", {"account": PAIR})
    bad = cli_paths / "accounts" / "x" / "state.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("[" * 100000, encoding="utf-8")
    cli.cmd_check()                        # must return, not raise
    assert "one login in" not in capsys.readouterr().out


def test_no_shared_login_note_when_every_dir_differs(cli_paths, capsys):
    write(cli_paths / "state.json", {"account": PAIR})
    write(cli_paths / "accounts" / "a" / "state.json", {"account": OTHER})
    write(cli_paths / "accounts" / "b" / "state.json", {})
    cli.cmd_check()
    assert "one login in" not in capsys.readouterr().out


# --- the hook ---------------------------------------------------------------------

def test_the_hook_never_opens_the_global_config(tmp_path, cfg, monkeypatch,
                                                opened):
    """A paced folder with a stale snapshot, so `run` takes both load sites
    and a refresh. `.claude.json` is planted where the CLI would read it."""
    write(cfg, {"oauthAccount": oauth()})
    work = norm_path("/work")
    monkeypatch.setattr(hook, "POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setattr(hook, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(hook, "HOOK_LOG_PATH", str(tmp_path / "hook.log"))
    write(tmp_path / "policy.json",
          {"paths": {work: {"paced": True, "enforce": "session"}},
           "defaults": {"chunk": 1}})
    now = int(time.time())
    bucket = {"session": {"pct": 1, "resets_epoch": now + 14400,
                          "window_seconds": 18000, "label": None}}
    write(tmp_path / "state.json",
          {"ts_epoch": now - 3600, "config_key": "", "buckets": bucket})

    def refresh():
        write(tmp_path / "state.json", {"ts_epoch": int(time.time()),
                                        "config_key": "", "account": PAIR,
                                        "buckets": bucket})
        return True
    monkeypatch.setattr(hook, "refresh_snapshot", refresh)
    opened.clear()

    assert hook.run(work) == (None, "line-caught-up")
    assert str(tmp_path / "state.json") in opened   # the spy saw the loads
    assert not [p for p in opened if p.endswith(".claude.json")]
